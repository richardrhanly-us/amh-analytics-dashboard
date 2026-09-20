<#
.SYNOPSIS
    Guided, enrollment-driven setup of the SortView Collector on a library's
    Tech Logic machine: extract the release, run .\setup.ps1, enter ONE
    short-lived enrollment code, and SortView does the rest. FROZEN release
    bundles only.

.DESCRIPTION
    Shipped at the root of a frozen release bundle as setup.ps1. Run it from
    an elevated, INTERACTIVE PowerShell session:

        .\setup.ps1

    The technician handles exactly one thing: the enrollment code a SortView
    administrator generated in Super Admin. Everything else -- the operational
    Customer ID, Branch ID, Installation ID and the permanent agent token --
    is issued by the SortView API in exchange for that code and used here
    automatically. None of those four values is ever asked for, printed,
    logged or written to collector_config.json (the config gets the three IDs;
    the token is never in it).

    WHAT IT DOES, IN ORDER (each stop names the step, what was left unchanged,
    and what to do next):

      0. Requires an elevated session, before anything else.
      1. VERIFIES THE BUNDLE FIRST, before it trusts anything in it (the API
         address included) and before any network request: it runs
         install.ps1 -VerifyBundleOnly, the existing manifest verification (the
         one implementation of it), so every file must match MANIFEST.json and no
         unlisted file may be present. Then: a frozen bundle, every sibling tool
         present, and the bundled runtime's own `version` command agreeing
         with MANIFEST.json (the canonical release version).
      2. Read-only machine checks: an existing install, Scheduled Task or
         SORTVIEW_API_TOKEN is never adopted, replaced or touched silently. A
         saved enrollment from an interrupted setup is recognised here (see
         RESUME).
      3. The three Tech Logic source files are located and must EXIST.
      4. The SortView API is reached over HTTPS (nothing is consumed by this).
         Steps 1-4 all happen BEFORE the enrollment code is requested, so an
         avoidable local problem never burns a single-use code.
      5. The enrollment code is read as a hidden SecureString and redeemed with
         POST /collector/enroll (sending the code, this computer's name and the
         bundle's canonical version). The response is validated strictly.
      6. The returned token is stored IMMEDIATELY as the Machine-scope
         SORTVIEW_API_TOKEN by tools\set-api-token.ps1 -- before installing
         anything -- and the non-secret enrollment details are saved for RESUME,
         read back and checked. Installing does not start until that record
         exists and verifies; if it cannot be saved or verified, setup stops here
         (the token stays stored).
      7. install.ps1 is run with the returned IDs (manifest verification,
         config, directories and every existing safety check stay canonical).
      8. tools\finish-install.ps1 is run with -UseExistingMachineToken: the
         interactive preflight, the SYSTEM-context preflight, the starting-
         cursor bootstrap (state must cover every source) and Scheduled Task
         registration -- DISABLED.
      9. Only then, and only on explicit confirmation, may the task be enabled.
         The saved enrollment details are removed once setup has completed.

    THE SCHEDULED TASK STAYS DISABLED unless step 9's explicit confirmation
    (or -EnableTask) happens after every step above succeeded. Pressing Enter
    does not enable it. On any failure this script also disables the task if
    anything left it enabled. It never starts a Collector run.

    SECRETS. The enrollment code is entered as a SecureString and converted to
    text only for the one request body, then cleared. The agent token is
    converted to a SecureString the moment the response is validated and the
    plain copy is cleared; it is handed to tools\set-api-token.ps1 as a
    SecureString parameter (in-process -- never a command-line argument). Neither
    value is written to the console, a file, a log or the config. The only file
    this script writes is the resume record above, which has no field that could
    hold either.

    RESUME. Enrollment is single-use, and the code is never made reusable -- so
    a local failure after the code was redeemed must not need a new one. Right
    after redemption the token is stored (Machine scope) and ONLY the non-secret
    details needed to continue are saved to
    <DataRoot>\setup\enrollment-recovery.json: the three IDs, the API address and
    the release version (never the enrollment code or the token -- the writer
    accepts nothing else). The folder is restricted to Administrators and SYSTEM
    before the file is written, and the record is then read back and must hold
    exactly the release version, API address and IDs just issued: saving and
    verifying it is a PREREQUISITE for install.ps1 (there is no "continue with a
    warning"; an unverified record is removed rather than left to drive a later
    resume). Running setup.ps1 again then finds that record
    and the stored token and offers to RESUME (Enter = resume): no enrollment
    request is made, and the saved IDs go to install.ps1 (or, if a matching
    install is already there, straight to tools\finish-install.ps1). The record is
    deleted when setup completes.
    Resuming never uses -Force and never adopts an install that does not match the
    saved enrollment. A record that is malformed, or a record whose token is gone,
    stops safely (nothing is touched or deleted) with instructions.

    RE-RUNS WITHOUT A SAVED ENROLLMENT. An existing install (files, config or
    Scheduled Task) is refused, never overwritten and never deleted: an
    unfinished manual setup is completed with tools\finish-install.ps1 (safe to
    re-run); a live install is updated with tools\update.ps1; starting over needs
    tools\uninstall.ps1 and a NEW enrollment code.

    SOURCES AND URL. The production API URL and the standard Tech Logic file
    locations come from this bundle's collector_config.example.json (the one
    canonical place they are recorded); -ApiUrl and -CheckinsPath/-RejectsPath/
    -AcsPath override them for development, tests and nonstandard installs.

    EXIT CODES: 0 = setup complete (task Disabled, or enabled by explicit
    request). 1 = a step failed or a requirement was not met. 2 = refused
    because existing state is unsafe to touch.

.PARAMETER EnrollmentCode
    Optional, for automation and tests only. A SecureString (a plain string
    cannot be passed by accident). Normal use omits it and is prompted, hidden.

.PARAMETER ApiUrl
    Override the bundle's production API URL (must be https://).

.PARAMETER CheckinsPath
.PARAMETER RejectsPath
.PARAMETER AcsPath
    Override the standard Tech Logic file locations. Every file must exist.

.PARAMETER InstallRoot
.PARAMETER DataRoot
    Same as install.ps1.

.PARAMETER ReplaceExistingToken
    Allow replacing a SORTVIEW_API_TOKEN already set on this machine (e.g. one
    another SortView component uses). Without it, an existing token is
    replaced only after an interactive confirmation.

.PARAMETER EnableTask
    After EVERY step succeeded, enable the Scheduled Task without asking.
    Never enables on any failure.

.EXAMPLE
    .\setup.ps1
#>

[CmdletBinding()]
param(
    [SecureString]$EnrollmentCode,
    [string]$ApiUrl,
    [string]$CheckinsPath,
    [string]$RejectsPath,
    [string]$AcsPath,
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$DataRoot = "C:\ProgramData\SortViewCollector",
    [switch]$ReplaceExistingToken,
    [switch]$EnableTask
)

$ErrorActionPreference = "Stop"

# The one Scheduled Task this Collector uses (tools\register-task.ps1).
$script:TaskName = "SortView Collector"
# The three source names the Collector's parser recognizes -- what install.ps1 writes.
$script:SourceNames = @("checkins", "rejects", "acs")
$script:ExpectedResponseKeys = @("customer_id", "branch_id", "installation_id", "agent_token")
# The complete, closed set of fields the resume record may contain. Nothing secret is on it.
$script:RecoveryKeys = @("schema_version", "created_utc", "release_version", "api_url", "customer_id", "branch_id", "installation_id")
# install.ps1 binds these as [Nullable[int]] (Int32); a larger value could not be passed.
$script:MaxInstallerId = [int]::MaxValue
$script:State = $null

# === side-effect wrappers ====================================================
# Everything that touches the machine, the network or the console goes through
# one of these small functions. The orchestration below never calls the
# underlying cmdlet directly, so tests can replace exactly the effect they are
# exercising (tests/test_collector_setup.py dot-sources this file and redefines
# them) while real runs use these.

function Test-IsAdministrator {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-LocalHostName {
    # Informational only (recorded in the new token's description). Never an identity.
    return [Environment]::MachineName
}

function Read-SetupAnswer {
    # A line of ordinary (visible, non-secret) input, or $null when there is no
    # way to ask -- a non-interactive host. Callers treat $null as "no answer".
    param([string]$Prompt)
    try {
        return (Read-Host $Prompt)
    } catch {
        return $null
    }
}

function Read-EnrollmentCodeSecure {
    # The code is typed hidden and never echoed. $null when no prompt is possible.
    try {
        return (Read-Host -AsSecureString -Prompt "Enter the enrollment code from your SortView administrator (input hidden)")
    } catch {
        return $null
    }
}

function ConvertFrom-SecureStringPlain {
    # The one place a SecureString becomes text. Callers clear the result.
    param([SecureString]$Secure)
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Get-Sha256Hex {
    param([string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))).Replace("-", "").ToLowerInvariant())
    } finally {
        $sha.Dispose()
    }
}

function Get-MachineToken {
    return [Environment]::GetEnvironmentVariable("SORTVIEW_API_TOKEN", "Machine")
}

function Get-SortViewTask {
    # $null when there is no such task. A failed lookup is thrown, never read as "absent".
    return (Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue)
}

function Enable-SortViewTask {
    Enable-ScheduledTask -TaskName $script:TaskName | Out-Null
}

function Disable-SortViewTask {
    Disable-ScheduledTask -TaskName $script:TaskName | Out-Null
}

function Invoke-SetupHttp {
    # One HTTPS request. Never throws: returns StatusCode (0 = none), Content
    # (the response body text, or $null) and Failure ($null, or a short
    # non-secret reason). Redirects are NOT followed -- a redirect would carry
    # the request body (the code) to wherever it points. Never logs a body.
    param([string]$Method, [string]$Url, [string]$Body)

    $result = [pscustomobject]@{ StatusCode = 0; Content = $null; Failure = $null }
    if ($Url -notmatch '^https://') {
        $result.Failure = "refused: the URL is not https://"
        return $result
    }
    try {
        # Windows PowerShell 5.1 does not always negotiate TLS 1.2 by default.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        # A newer runtime that manages this itself.
        $null = $_
    }
    try {
        $request = @{
            Uri = $Url; Method = $Method; UseBasicParsing = $true; TimeoutSec = 30
            MaximumRedirection = 0; ErrorAction = "Stop"
        }
        if ($Method -eq "Post") {
            $request["Body"] = $Body
            $request["ContentType"] = "application/json"
        }
        $response = Invoke-WebRequest @request
        $result.StatusCode = [int]$response.StatusCode
        $result.Content = [string]$response.Content
    } catch {
        $response = $_.Exception.Response
        if ($null -ne $response) {
            $result.StatusCode = [int]$response.StatusCode
            $result.Failure = "HTTP $($result.StatusCode)"
        } else {
            $result.Failure = "no response ($($_.Exception.GetType().Name))"
        }
    }
    return $result
}

function Invoke-SetupTool {
    # Runs one of this bundle's own scripts IN-PROCESS (so a SecureString parameter
    # never appears on a command line) with named parameters, showing its output.
    # Returns its exit code; a script that ends without `exit` and does not throw
    # is a success (set-api-token.ps1 works that way), a thrown error is a failure.
    param([string]$Path, [hashtable]$Parameters)
    $global:LASTEXITCODE = $null
    try {
        & $Path @Parameters | Out-Host
    } catch {
        Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
        return 1
    }
    if ($null -eq $global:LASTEXITCODE) { return 0 }
    return [int]$global:LASTEXITCODE
}

function Get-RuntimeVersion {
    # Asks the bundled frozen runtime what version it actually is (its config-free
    # `version` command). Throws unless it exits 0 and prints exactly one version.
    param([string]$ExePath)
    $lines = $null
    $global:LASTEXITCODE = $null
    try {
        $lines = @(& $ExePath version)
    } catch {
        throw "could not run '$ExePath version': $($_.Exception.Message)"
    }
    if ($null -ne $global:LASTEXITCODE -and $global:LASTEXITCODE -ne 0) {
        throw "'$ExePath version' exited with code $($global:LASTEXITCODE)"
    }
    $text = (($lines | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { throw "'$ExePath version' printed no version" }
    if ($text -notmatch '^[0-9A-Za-z][0-9A-Za-z.+\-]*$') { throw "'$ExePath version' printed something that is not a version: '$text'" }
    return $text
}

# === pure helpers (touch nothing; exercised directly by the tests) =============

function Test-EnrollmentApiUrl {
    # $null when the URL is acceptable, otherwise the reason. HTTPS only, a
    # host, no credentials, no query/fragment.
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url)) { return "no API URL" }
    $trimmed = $Url.Trim()
    if ($trimmed -notmatch '^https://') { return "the API URL must begin with https:// (got '$trimmed')" }
    if ($trimmed -notmatch '^https://[^/\s?#@]+(/[^\s?#]*)?$') {
        return "the API URL must be https://<host>[/path] with no credentials, query or fragment (got '$trimmed')"
    }
    return $null
}

function Get-BundleDefaults {
    # The production API URL and the standard Tech Logic paths, from the bundle's
    # collector_config.example.json -- the one canonical place they are recorded.
    # Returns ApiUrl, Sources (name -> path) and Problems (empty when valid).
    param([string]$BundleRoot)
    $problems = @()
    $apiUrl = $null
    $sources = @{}
    $path = Join-Path $BundleRoot "collector_config.example.json"
    $doc = $null
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $problems += "collector_config.example.json is missing from the bundle."
    } else {
        try {
            $doc = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            $problems += "collector_config.example.json is not valid JSON."
        }
    }
    if ($null -ne $doc) {
        $urlProblem = Test-EnrollmentApiUrl -Url ([string]$doc.api_url)
        if ($null -ne $urlProblem) { $problems += "the bundle's api_url is unusable: $urlProblem" } else { $apiUrl = ([string]$doc.api_url).Trim().TrimEnd('/') }
        foreach ($entry in @($doc.sources)) {
            if ($null -ne $entry -and -not [string]::IsNullOrWhiteSpace([string]$entry.name)) { $sources[[string]$entry.name] = [string]$entry.path }
        }
        foreach ($name in $script:SourceNames) {
            if (-not $sources.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($sources[$name])) {
                $problems += "the bundle lists no standard path for source '$name'."
            }
        }
    }
    return [pscustomobject]@{ ApiUrl = $apiUrl; Sources = $sources; Problems = $problems }
}

function ConvertTo-EnrollmentResult {
    # STRICT validation of a 200 response body. Valid only when the body is a JSON
    # object with EXACTLY customer_id, branch_id, installation_id and agent_token;
    # the three IDs are genuine positive integers the installer can take, and the
    # token is a plausible URL-safe token. Anything else is rejected whole.
    param([string]$Content)
    $fail = { param($reason) return [pscustomobject]@{ Valid = $false; Problem = $reason; CustomerId = $null; BranchId = $null; InstallationId = $null; AgentToken = $null } }

    if ([string]::IsNullOrWhiteSpace($Content)) { return (& $fail "the response body is empty") }
    $doc = $null
    try {
        $doc = $Content | ConvertFrom-Json
    } catch {
        return (& $fail "the response body is not valid JSON")
    }
    if ($doc -isnot [System.Management.Automation.PSCustomObject]) { return (& $fail "the response body is not a JSON object") }

    $names = @($doc.PSObject.Properties | ForEach-Object { $_.Name })
    foreach ($required in $script:ExpectedResponseKeys) {
        if (-not ($names -ccontains $required)) { return (& $fail "the response is missing '$required'") }
    }
    if ($names.Count -ne $script:ExpectedResponseKeys.Count) { return (& $fail "the response has unexpected fields") }

    $ids = @{}
    foreach ($name in @("customer_id", "branch_id", "installation_id")) {
        $value = $doc.PSObject.Properties[$name].Value
        if (($value -isnot [int] -and $value -isnot [long]) -or $value -lt 1) { return (& $fail "'$name' is not a positive integer") }
        if ($value -gt $script:MaxInstallerId) { return (& $fail "'$name' is larger than the installer accepts") }
        $ids[$name] = [int]$value
    }

    $token = $doc.PSObject.Properties["agent_token"].Value
    if ($token -isnot [string] -or $token -notmatch '^[A-Za-z0-9_\-]{20,256}$') { return (& $fail "'agent_token' is not a plausible token") }

    return [pscustomobject]@{
        Valid = $true; Problem = $null
        CustomerId = $ids["customer_id"]; BranchId = $ids["branch_id"]; InstallationId = $ids["installation_id"]
        AgentToken = $token
    }
}

function Get-ExistingInstallProblem {
    # $null when this machine has no Collector footprint (a first install), else
    # what was found. Kind = Live (a Scheduled Task exists and is not Disabled) or
    # Partial (files and/or a Disabled task are present).
    param([string]$InstallRoot, [string]$DataRoot)
    $found = @()
    if (Test-Path -LiteralPath (Join-Path $InstallRoot "SortViewCollector.exe")) { $found += "the Collector runtime in '$InstallRoot'" }
    if (Test-Path -LiteralPath (Join-Path $InstallRoot ".venv\Scripts\python.exe")) { $found += "a Python-based Collector install in '$InstallRoot'" }
    if (Test-Path -LiteralPath (Join-Path $DataRoot "config\collector_config.json")) { $found += "a Collector config in '$DataRoot\config'" }
    $task = Get-SortViewTask
    $taskState = $null
    if ($null -ne $task) {
        $taskState = [string]$task.State
        $found += "the '$($script:TaskName)' Scheduled Task (state: $taskState)"
    }
    if ($found.Count -eq 0) { return $null }
    $kind = if ($null -ne $task -and $taskState -ne "Disabled") { "Live" } else { "Partial" }
    return [pscustomobject]@{ Kind = $kind; Found = $found }
}

function Test-AbsoluteSourcePath {
    # Same rule as install.ps1: a drive-rooted or UNC path.
    param([string]$Path)
    return (-not [string]::IsNullOrWhiteSpace($Path)) -and ($Path.Trim() -match '^([A-Za-z]:[\\/]|\\\\[^\\/])')
}

# === the resume record (non-secret) ==========================================================

function Get-SetupRecoveryPath {
    param([string]$DataRoot)
    return (Join-Path (Join-Path $DataRoot "setup") "enrollment-recovery.json")
}

function ConvertTo-SetupRecoveryJson {
    # The ONLY way the record's content is produced. Every field is typed and named here,
    # and there is no parameter that could carry the enrollment code or the token.
    param([int]$CustomerId, [int]$BranchId, [int]$InstallationId, [string]$ApiUrl, [string]$ReleaseVersion)
    $record = [ordered]@{
        schema_version  = 1
        created_utc     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        release_version = $ReleaseVersion
        api_url         = $ApiUrl
        customer_id     = $CustomerId
        branch_id       = $BranchId
        installation_id = $InstallationId
    }
    return ($record | ConvertTo-Json)
}

function Test-SetupRecoveryContent {
    # STRICT validation of a saved record: exactly the allowed fields (a stray extra
    # one -- such as anything token-shaped -- makes it invalid), the IDs genuine
    # positive integers the installer can take, an https:// address.
    param([string]$Content)
    $fail = { param($list) return [pscustomobject]@{ Valid = $false; Problems = @($list); Record = $null } }

    if ([string]::IsNullOrWhiteSpace($Content)) { return (& $fail @("the file is empty")) }
    $doc = $null
    try {
        $doc = $Content | ConvertFrom-Json
    } catch {
        return (& $fail @("the file is not valid JSON"))
    }
    if ($doc -isnot [System.Management.Automation.PSCustomObject]) { return (& $fail @("the document is not a JSON object")) }

    $problems = @()
    $names = @($doc.PSObject.Properties | ForEach-Object { $_.Name })
    foreach ($key in $script:RecoveryKeys) {
        if (-not ($names -ccontains $key)) { $problems += "'$key' is missing" }
    }
    if ($names.Count -ne $script:RecoveryKeys.Count) { $problems += "it has unexpected fields (only these are allowed: $($script:RecoveryKeys -join ', '))" }
    if ($problems.Count -gt 0) { return (& $fail $problems) }

    $schema = $doc.schema_version
    if (($schema -isnot [int] -and $schema -isnot [long]) -or $schema -ne 1) { $problems += "'schema_version' must be 1" }
    $ids = @{}
    foreach ($name in @("customer_id", "branch_id", "installation_id")) {
        $value = $doc.PSObject.Properties[$name].Value
        if (($value -isnot [int] -and $value -isnot [long]) -or $value -lt 1 -or $value -gt $script:MaxInstallerId) {
            $problems += "'$name' is not a positive integer the installer accepts"
        } else {
            $ids[$name] = [int]$value
        }
    }
    $apiUrl = $null
    if ($doc.api_url -isnot [string]) {
        $problems += "'api_url' is not text"
    } else {
        $urlProblem = Test-EnrollmentApiUrl -Url $doc.api_url
        if ($null -ne $urlProblem) { $problems += "'api_url' is unusable: $urlProblem" } else { $apiUrl = $doc.api_url.Trim().TrimEnd('/') }
    }
    if ($doc.release_version -isnot [string] -or $doc.release_version -notmatch '^[0-9A-Za-z][0-9A-Za-z.+\-]*$') { $problems += "'release_version' is not a version" }
    if ($doc.created_utc -isnot [string] -or $doc.created_utc -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') { $problems += "'created_utc' is not a UTC timestamp" }
    if ($problems.Count -gt 0) { return (& $fail $problems) }

    return [pscustomobject]@{
        Valid = $true; Problems = @()
        Record = [pscustomobject]@{
            CustomerId = $ids["customer_id"]; BranchId = $ids["branch_id"]; InstallationId = $ids["installation_id"]
            ApiUrl = $apiUrl; ReleaseVersion = [string]$doc.release_version; CreatedUtc = [string]$doc.created_utc
        }
    }
}

function Get-SetupRecovery {
    # Absent (no record), Valid (with Record), or Malformed (with Problems). Reads only.
    param([string]$DataRoot)
    $path = Get-SetupRecoveryPath -DataRoot $DataRoot
    $outcome = { param($status, $problems, $record) return [pscustomobject]@{ Status = $status; Path = $path; Problems = @($problems); Record = $record } }
    if (-not (Test-Path -LiteralPath $path)) { return (& $outcome "Absent" @() $null) }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return (& $outcome "Malformed" @("it is not a file") $null) }
    $content = $null
    try {
        $content = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    } catch {
        return (& $outcome "Malformed" @("it could not be read: $($_.Exception.Message)") $null)
    }
    $check = Test-SetupRecoveryContent -Content $content
    if (-not $check.Valid) { return (& $outcome "Malformed" $check.Problems $null) }
    return (& $outcome "Valid" @() $check.Record)
}

function Protect-SetupRecoveryDirectory {
    # Restricts a folder to Administrators and SYSTEM only: inheritance from the
    # parent is cut (no Users / Everyone / Authenticated Users) and both grants
    # flow to whatever is created inside. Well-known SIDs, so it does not depend on
    # the machine's language. Read back and verified: a folder that could not be
    # locked down is an error, and the caller then writes nothing into it.
    param([string]$Path)
    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) {
        $identity = New-Object System.Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "ContainerInherit, ObjectInherit", "None", "Allow")
        $security.AddAccessRule($rule)
    }
    # Only the access list is written. (Set-Acl would also write the -- unset -- owner and group,
    # which needs an account lookup that fails on a domain-joined machine whose domain is unreachable.)
    $directory = New-Object System.IO.DirectoryInfo($Path)
    if ($directory.PSObject.Methods["SetAccessControl"]) {
        $directory.SetAccessControl($security)
    } else {
        [System.IO.FileSystemAclExtensions]::SetAccessControl($directory, $security)
    }

    $applied = Get-Acl -LiteralPath $Path
    $sids = @($applied.Access | ForEach-Object { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique)
    if (-not $applied.AreAccessRulesProtected -or ($sids -join ",") -ne "S-1-5-18,S-1-5-32-544") {
        throw "the folder '$Path' could not be restricted to Administrators and SYSTEM"
    }
}

function Save-SetupRecovery {
    # Creates <DataRoot>\setup, locks it down FIRST, and only then writes the record
    # into it. Returns the record's path; throws on any failure.
    param([string]$DataRoot, [string]$Json)
    $directory = Join-Path $DataRoot "setup"
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    Protect-SetupRecoveryDirectory -Path $directory
    $path = Get-SetupRecoveryPath -DataRoot $DataRoot
    [System.IO.File]::WriteAllText($path, $Json, (New-Object System.Text.UTF8Encoding($false)))
    return $path
}

function Confirm-SetupRecovery {
    # Reads the record back from disk -- through the same strict reader a resume uses -- and
    # checks that it holds exactly what was just enrolled. Returns the problems (none = verified).
    param([string]$DataRoot, [int]$CustomerId, [int]$BranchId, [int]$InstallationId, [string]$ApiUrl, [string]$ReleaseVersion)
    $read = Get-SetupRecovery -DataRoot $DataRoot
    if ($read.Status -eq "Absent") { return @("the record is not there after it was written") }
    if ($read.Status -ne "Valid") { return @("the record read back is not valid: $($read.Problems -join '; ')") }
    $problems = @()
    $saved = $read.Record
    if ($saved.ReleaseVersion -cne $ReleaseVersion) { $problems += "the saved release version differs from the one enrolled" }
    if ($saved.ApiUrl -cne $ApiUrl.Trim().TrimEnd('/')) { $problems += "the saved API address differs from the one enrolled" }
    if ($saved.CustomerId -ne $CustomerId) { $problems += "the saved customer ID differs from the one issued" }
    if ($saved.BranchId -ne $BranchId) { $problems += "the saved branch ID differs from the one issued" }
    if ($saved.InstallationId -ne $InstallationId) { $problems += "the saved installation ID differs from the one issued" }
    return $problems
}

function Remove-SetupRecovery {
    # Deletes exactly the record file and then its folder if (and only if) the folder
    # is empty -- never recursive, never anything else.
    param([string]$DataRoot)
    $path = Get-SetupRecoveryPath -DataRoot $DataRoot
    if (Test-Path -LiteralPath $path -PathType Leaf) { [System.IO.File]::Delete($path) }
    $directory = Split-Path -Parent $path
    if ((Test-Path -LiteralPath $directory -PathType Container) -and @(Get-ChildItem -LiteralPath $directory -Force).Count -eq 0) {
        [System.IO.Directory]::Delete($directory)
    }
}

function Test-InstallMatchesRecovery {
    # Does the install already on this machine belong to THIS saved enrollment: the
    # runtime is there and collector_config.json carries the same three IDs and API
    # address? Anything else is somebody else's install (or a partial one) and is
    # never adopted. Reads only.
    param([string]$InstallRoot, [string]$ConfigPath, $Record)
    $result = { param($isMatch, $reason) return [pscustomobject]@{ Matches = $isMatch; Reason = $reason } }
    if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot "SortViewCollector.exe") -PathType Leaf)) { return (& $result $false "the Collector runtime is not there") }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { return (& $result $false "there is no collector_config.json (the install is incomplete)") }
    $config = $null
    try {
        $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return (& $result $false "collector_config.json could not be read")
    }
    foreach ($pair in @(@("customer_id", $Record.CustomerId), @("branch_id", $Record.BranchId), @("installation_id", $Record.InstallationId))) {
        $value = $config.PSObject.Properties[$pair[0]].Value
        if (($value -isnot [int] -and $value -isnot [long]) -or [long]$value -ne [long]$pair[1]) {
            return (& $result $false "collector_config.json has a different $($pair[0])")
        }
    }
    $configUrl = ([string]$config.api_url).Trim().TrimEnd('/')
    if ($configUrl -ne $Record.ApiUrl) { return (& $result $false "collector_config.json has a different api_url") }
    return (& $result $true "the installed config matches the saved enrollment")
}

function Get-EnrollmentStateLines {
    # What the operator needs to know about the single-use code, the token and the saved
    # enrollment, given how far this run got. Shown on every stop.
    $lines = @()
    if ($null -ne $script:State -and $script:State.CodeConsumed) {
        $lines += "The one-time enrollment code HAS BEEN USED and cannot be used again."
        if ($script:State.TokenStored) {
            $lines += "The permanent API token IS stored on this machine (Machine scope; never shown)."
            if ($script:State.RecoveryOnDisk) {
                $lines += "The non-secret enrollment details are saved at '$($script:State.RecoveryPath)': run setup.ps1 again to RESUME -- no new enrollment code is needed."
            } else {
                $lines += "The enrollment details could NOT be saved, so this setup cannot be resumed: ask your SortView administrator for a NEW enrollment code."
            }
        } else {
            $lines += "The permanent API token was NOT stored. Ask your SortView administrator for a NEW enrollment code."
        }
    } elseif ($null -ne $script:State -and $script:State.CodeAttempted) {
        $lines += "The enrollment code may or may not have been used. If the next attempt is refused, ask for a new one."
    } else {
        $lines += "The enrollment code was NOT used."
    }
    $lines += "The Scheduled Task has not been enabled."
    return $lines
}

function Stop-Setup {
    # One place for every stop: names the step, what was left unchanged, what to do
    # -- then ends the run with an exit code. Thrown (not `exit`) so the caller's
    # finally always runs, and so the tests can drive the whole flow in-process.
    param(
        [Parameter(Mandatory)][string]$Step,
        [Parameter(Mandatory)][string]$Problem,
        [Parameter(Mandatory)][string]$Unchanged,
        [Parameter(Mandatory)][string]$Fix,
        [int]$ExitCode = 1
    )
    Write-Host ""
    Write-Host "SETUP STOPPED at $Step" -ForegroundColor Red
    Write-Host "  What went wrong:  $Problem" -ForegroundColor Red
    Write-Host "  Left unchanged:   $Unchanged"
    foreach ($line in (Get-EnrollmentStateLines)) { Write-Host "  $line" }
    Write-Host "  What to do:       $Fix"
    $stop = New-Object System.Exception "setup stopped at $Step"
    $stop.Data["SortViewSetupExitCode"] = $ExitCode
    throw $stop
}

# === enrollment ====================================================================

function Invoke-Enrollment {
    # Redeems the code. Returns Outcome (Enrolled / Rejected / RateLimited /
    # Unreachable / Redirected / BadResponse / ServerError), a non-secret Detail,
    # and -- only when Enrolled -- the three IDs, the token as a SecureString and
    # the SHA-256 of the token (to verify what was stored). The plain token and
    # the plain code never leave this function.
    param([string]$ApiUrl, [SecureString]$Code, [string]$HostName, [string]$Version)

    $outcome = { param($name, $detail) return [pscustomobject]@{ Outcome = $name; Detail = $detail; CustomerId = $null; BranchId = $null; InstallationId = $null; Token = $null; TokenHash = $null } }

    $plainCode = ConvertFrom-SecureStringPlain -Secure $Code
    $body = $null
    try {
        $body = (@{ enrollment_code = $plainCode; hostname = $HostName; collector_version = $Version } | ConvertTo-Json -Compress)
    } finally {
        $plainCode = $null
    }
    $http = $null
    try {
        $http = Invoke-SetupHttp -Method "Post" -Url "$ApiUrl/collector/enroll" -Body $body
    } finally {
        $body = $null
    }

    switch ($http.StatusCode) {
        200 {
            $parsed = ConvertTo-EnrollmentResult -Content $http.Content
            $http.Content = $null
            if (-not $parsed.Valid) {
                return (& $outcome "BadResponse" "SortView answered, but the response was not valid ($($parsed.Problem)).")
            }
            $secureToken = ConvertTo-SecureString -String $parsed.AgentToken -AsPlainText -Force
            $tokenHash = Get-Sha256Hex -Text $parsed.AgentToken
            $result = & $outcome "Enrolled" "enrolled"
            $result.CustomerId = $parsed.CustomerId
            $result.BranchId = $parsed.BranchId
            $result.InstallationId = $parsed.InstallationId
            $result.Token = $secureToken
            $result.TokenHash = $tokenHash
            $parsed.AgentToken = $null
            $parsed = $null
            return $result
        }
        400 { return (& $outcome "Rejected" "The enrollment code was not accepted (invalid, expired, or already used).") }
        429 { return (& $outcome "RateLimited" "SortView is limiting requests from this computer.") }
    }
    if ($http.StatusCode -ge 300 -and $http.StatusCode -lt 400) {
        return (& $outcome "Redirected" "SortView redirected the request (HTTP $($http.StatusCode)); it was not followed.")
    }
    if ($http.StatusCode -ge 500) {
        return (& $outcome "ServerError" "SortView reported an error (HTTP $($http.StatusCode)).")
    }
    if ($http.StatusCode -eq 0) {
        return (& $outcome "Unreachable" "Could not complete a secure connection ($($http.Failure)).")
    }
    return (& $outcome "BadResponse" "SortView answered with an unexpected status (HTTP $($http.StatusCode)).")
}

function Test-ApiReachable {
    # A side-effect-free GET of the API root over HTTPS, BEFORE the code is
    # requested: catches TLS, proxy and DNS problems without consuming anything.
    param([string]$ApiUrl)
    $http = Invoke-SetupHttp -Method "Get" -Url "$ApiUrl/" -Body $null
    return [pscustomobject]@{ Ok = ($http.StatusCode -eq 200); Detail = $http.Failure; StatusCode = $http.StatusCode }
}

# === source files ==============================================================================

function Get-SourceSelection {
    # Decides the three source file paths and PROVES they exist. The standard
    # locations are offered as the default; -CheckinsPath/-RejectsPath/-AcsPath
    # override any of them. A missing file is never accepted: it is asked for again
    # (interactively) or the run stops.
    param($Defaults, [string]$Checkins, [string]$Rejects, [string]$Acs)

    $explicit = @{ checkins = $Checkins; rejects = $Rejects; acs = $Acs }
    $chosen = [ordered]@{}
    $anyExplicit = $false
    foreach ($name in $script:SourceNames) {
        if (-not [string]::IsNullOrWhiteSpace($explicit[$name])) {
            $chosen[$name] = $explicit[$name].Trim()
            $anyExplicit = $true
        } else {
            $chosen[$name] = [string]$Defaults.Sources[$name]
        }
    }

    $allStandardPresent = $true
    foreach ($name in $script:SourceNames) {
        if (-not (Test-Path -LiteralPath $chosen[$name] -PathType Leaf)) { $allStandardPresent = $false }
    }

    # Offer the standard locations when nothing was overridden and they are all there.
    $useAsIs = $true
    if (-not $anyExplicit -and $allStandardPresent) {
        Write-Host "Found the standard Tech Logic files:"
        foreach ($name in $script:SourceNames) { Write-Host "    $name : $($chosen[$name])" }
        $answer = Read-SetupAnswer -Prompt "Use these files? [Y/n] (Enter = yes)"
        if ($null -ne $answer -and @("n", "no") -contains $answer.Trim().ToLowerInvariant()) { $useAsIs = $false }
    }

    $final = [ordered]@{}
    foreach ($name in $script:SourceNames) {
        $path = $chosen[$name]
        if (-not $useAsIs) { $path = "" }
        $attempts = 0
        while ($true) {
            if ((Test-AbsoluteSourcePath -Path $path) -and (Test-Path -LiteralPath $path -PathType Leaf)) { break }
            if (-not [string]::IsNullOrWhiteSpace($path)) {
                if (-not (Test-AbsoluteSourcePath -Path $path)) {
                    Write-Host "  '$path' is not an absolute path (like C:\folder\file.txt)." -ForegroundColor Yellow
                } else {
                    Write-Host "  The $name file was not found: $path" -ForegroundColor Yellow
                }
            }
            $attempts++
            $answer = $null
            if ($attempts -le 3) { $answer = Read-SetupAnswer -Prompt "Full path to the $name file (Enter alone to stop)" }
            if ([string]::IsNullOrWhiteSpace($answer)) {
                Stop-Setup -Step "step 3 (source files)" -Problem "The $name source file was not found and none was provided: '$path'." `
                    -Unchanged "nothing was installed and the enrollment code was not requested" `
                    -Fix "Make sure the Tech Logic machine has the file (or pass -CheckinsPath / -RejectsPath / -AcsPath with the real location), then run setup.ps1 again." -ExitCode 1
            }
            $path = $answer.Trim()
        }
        $final[$name] = $path
    }
    return $final
}

# === the run ==================================================================================

function Invoke-SetupMain {
    [CmdletBinding()]
    param(
        [SecureString]$EnrollmentCode,
        [string]$ApiUrl,
        [string]$CheckinsPath,
        [string]$RejectsPath,
        [string]$AcsPath,
        [string]$InstallRoot = "C:\SortView\Collector",
        [string]$DataRoot = "C:\ProgramData\SortViewCollector",
        [switch]$ReplaceExistingToken,
        [switch]$EnableTask
    )

    $script:State = @{
        CodeAttempted = $false; CodeConsumed = $false; TokenStored = $false
        RecoveryOnDisk = $false; RecoveryPath = $null
        # Set just before anything is installed or finished: only from then on can a Scheduled Task be
        # this run's own. Before that, a task that exists is the customer's and is never touched.
        InstallStarted = $false
        TaskEnabledOnPurpose = $false
    }
    $exitCode = 1
    $enrollment = $null
    try {
        $bundleRoot = $PSScriptRoot

        # === STEP 0: elevation, before anything else ================================
        if (-not (Test-IsAdministrator)) {
            Stop-Setup -Step "step 0 (checks)" -Problem "This script must be run from an elevated (Administrator) PowerShell session." `
                -Unchanged "nothing was touched" -Fix "Open PowerShell with 'Run as administrator', go to this folder, and run .\setup.ps1 again." -ExitCode 1
        }

        Write-Host "=== SortView Collector: guided setup ===" -ForegroundColor Cyan
        Write-Host "You will need the one-time enrollment code from your SortView administrator."
        Write-Host ""

        # === STEP 1: the bundle -- verified FIRST, before anything in it is trusted ===
        Write-Host "=== Step 1: verifying this release bundle ===" -ForegroundColor Cyan
        $runtimeExe = Join-Path $bundleRoot "runtime\SortViewCollector.exe"
        $installScript = Join-Path $bundleRoot "install.ps1"
        $toolsDir = Join-Path $bundleRoot "tools"
        $setTokenScript = Join-Path $toolsDir "set-api-token.ps1"
        $finishScript = Join-Path $toolsDir "finish-install.ps1"
        $noNetworkNoCode = "nothing was installed or changed, no network request was made, and the enrollment code was not requested"

        if (-not (Test-Path -LiteralPath $installScript -PathType Leaf)) {
            Stop-Setup -Step "step 1 (bundle verification)" -Problem "install.ps1 is missing from this bundle, so the bundle cannot be verified." `
                -Unchanged $noNetworkNoCode -Fix "Re-copy the complete release bundle, then run setup.ps1 again." -ExitCode 1
        }
        # The existing, canonical manifest verification (read-only, needs no IDs): every file must
        # match MANIFEST.json and no unlisted file may be present.
        $toolExit = Invoke-SetupTool -Path $installScript -Parameters @{ VerifyBundleOnly = $true }
        if ($toolExit -ne 0) {
            Stop-Setup -Step "step 1 (bundle verification)" -Problem "This release bundle failed verification against MANIFEST.json (exit code $toolExit) -- see the message above. It may be damaged, incomplete, or modified." `
                -Unchanged $noNetworkNoCode -Fix "Do not use this bundle. Re-download or re-copy the complete release bundle, then run setup.ps1 again." -ExitCode 1
        }
        Write-Host "  OK: every file in the bundle matches MANIFEST.json." -ForegroundColor Green

        if ((Test-Path -LiteralPath (Join-Path $bundleRoot "collector")) -or -not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) {
            Stop-Setup -Step "step 1 (bundle)" -Problem "This is not a frozen SortView Collector release bundle (setup.ps1 supports frozen bundles only)." `
                -Unchanged $noNetworkNoCode -Fix "Use the release bundle whose folder contains runtime\SortViewCollector.exe." -ExitCode 1
        }
        foreach ($required in @($setTokenScript, $finishScript, (Join-Path $toolsDir "preflight-system.ps1"), (Join-Path $toolsDir "register-task.ps1"))) {
            if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
                Stop-Setup -Step "step 1 (bundle)" -Problem "This release bundle is incomplete: '$required' is missing." `
                    -Unchanged $noNetworkNoCode -Fix "Re-copy the complete release bundle, then run setup.ps1 again." -ExitCode 1
            }
        }

        $manifestVersion = $null
        try {
            $manifestVersion = [string]((Get-Content -LiteralPath (Join-Path $bundleRoot "MANIFEST.json") -Raw -Encoding UTF8 | ConvertFrom-Json).version)
        } catch {
            Stop-Setup -Step "step 1 (bundle)" -Problem "MANIFEST.json could not be read: $($_.Exception.Message)" `
                -Unchanged $noNetworkNoCode -Fix "Re-copy the complete release bundle." -ExitCode 1
        }
        $runtimeVersion = $null
        try {
            $runtimeVersion = Get-RuntimeVersion -ExePath $runtimeExe
        } catch {
            Stop-Setup -Step "step 1 (bundle)" -Problem "The bundled runtime would not report its version: $($_.Exception.Message)" `
                -Unchanged $noNetworkNoCode -Fix "Re-copy the release bundle (it may be damaged or blocked by antivirus)." -ExitCode 1
        }
        if ($runtimeVersion -cne $manifestVersion) {
            Stop-Setup -Step "step 1 (bundle)" -Problem "The bundled runtime reports version '$runtimeVersion' but the release manifest says '$manifestVersion'." `
                -Unchanged $noNetworkNoCode -Fix "Do not use this bundle; obtain a correct release." -ExitCode 1
        }

        # Only now -- the bundle is verified -- is anything in it (the API address) read.
        $defaults = Get-BundleDefaults -BundleRoot $bundleRoot
        if ($defaults.Problems.Count -gt 0) {
            foreach ($problem in $defaults.Problems) { Write-Host "  $problem" -ForegroundColor Red }
            Stop-Setup -Step "step 1 (bundle)" -Problem "The bundle's defaults are unusable (listed above)." `
                -Unchanged $noNetworkNoCode -Fix "Re-copy the complete release bundle." -ExitCode 1
        }
        $resolvedApiUrl = $defaults.ApiUrl
        if (-not [string]::IsNullOrWhiteSpace($ApiUrl)) {
            $urlProblem = Test-EnrollmentApiUrl -Url $ApiUrl
            if ($null -ne $urlProblem) {
                Stop-Setup -Step "step 1 (bundle)" -Problem "-ApiUrl is not acceptable: $urlProblem." `
                    -Unchanged $noNetworkNoCode -Fix "Pass an https:// URL, or omit -ApiUrl to use the bundle's production address." -ExitCode 1
            }
            $resolvedApiUrl = $ApiUrl.Trim().TrimEnd('/')
        }
        Write-Host "  OK: frozen bundle, release version $runtimeVersion." -ForegroundColor Green

        # === STEP 2: this machine (read-only) ==========================================
        Write-Host ""
        Write-Host "=== Step 2: checking this machine ===" -ForegroundColor Cyan
        $existing = $null
        try {
            $existing = Get-ExistingInstallProblem -InstallRoot $InstallRoot -DataRoot $DataRoot
        } catch {
            Stop-Setup -Step "step 2 (machine)" -Problem "Could not inspect this machine for an existing install: $($_.Exception.Message)" `
                -Unchanged "nothing was touched and the enrollment code was not requested" -Fix "Resolve the error above, then run setup.ps1 again." -ExitCode 2
        }
        if ($null -ne $existing -and $existing.Kind -eq "Live") {
            foreach ($item in $existing.Found) { Write-Host "  Found: $item" -ForegroundColor Yellow }
            Stop-Setup -Step "step 2 (machine)" -Problem "A running or enabled SortView Collector already exists on this computer (listed above); setup will not touch it." `
                -Unchanged "the existing install, its state and its Scheduled Task, and the enrollment code was not requested" `
                -Fix "This computer already has a running or enabled Collector. setup.ps1 is for first installs only: update it with tools\update.ps1. Nothing was changed." -ExitCode 2
        }

        $recovery = Get-SetupRecovery -DataRoot $DataRoot
        $existingToken = Get-MachineToken
        $tokenPresent = -not [string]::IsNullOrWhiteSpace($existingToken)
        $existingToken = $null
        $mode = "Fresh"
        $customerId = $null
        $branchId = $null
        $installationId = $null
        $configPath = Join-Path $DataRoot "config\collector_config.json"

        if ($recovery.Status -eq "Malformed") {
            foreach ($problem in $recovery.Problems) { Write-Host "  $problem" -ForegroundColor Red }
            Stop-Setup -Step "step 2 (machine)" -Problem "A saved enrollment record exists at '$($recovery.Path)' but it is not valid (listed above); setup will not guess." `
                -Unchanged "the record, the installed files, the token and the Scheduled Task, and the enrollment code was not requested" `
                -Fix "The record holds only non-secret IDs. If you are sure it is stale, delete that one file and run setup.ps1 again (a NEW enrollment code will then be needed); otherwise contact SortView support." -ExitCode 2
        }

        if ($recovery.Status -eq "Valid") {
            if (-not $tokenPresent) {
                Stop-Setup -Step "step 2 (machine)" -Problem "A saved enrollment was found (installation $($recovery.Record.InstallationId)), but this computer has no API token (SORTVIEW_API_TOKEN, Machine scope) to go with it." `
                    -Unchanged "the saved record and everything else, and the enrollment code was not requested" `
                    -Fix "The token cannot be recovered and the used enrollment code cannot be reused. Ask your SortView administrator for a NEW enrollment code, delete the saved record '$($recovery.Path)' (non-secret IDs only), then run setup.ps1 again." -ExitCode 2
            }
            # From here, this run is a continuation of an earlier one whose code was already redeemed.
            $script:State.CodeConsumed = $true
            $script:State.TokenStored = $true
            $script:State.RecoveryOnDisk = $true
            $script:State.RecoveryPath = $recovery.Path
            $record = $recovery.Record

            # The token was issued by THAT server: never point a resumed setup anywhere else.
            if (-not [string]::IsNullOrWhiteSpace($ApiUrl) -and $ApiUrl.Trim().TrimEnd('/') -cne $record.ApiUrl) {
                Stop-Setup -Step "step 2 (machine)" -Problem "-ApiUrl differs from the API address this enrollment was made with ('$($record.ApiUrl)')." `
                    -Unchanged "everything on this computer" -Fix "Omit -ApiUrl to resume, or start over with a NEW enrollment code (delete '$($recovery.Path)' first)." -ExitCode 2
            }
            $resolvedApiUrl = $record.ApiUrl

            if ($null -eq $existing) {
                $mode = "ResumeInstall"
            } else {
                foreach ($item in $existing.Found) { Write-Host "  Found: $item" -ForegroundColor Yellow }
                $match = Test-InstallMatchesRecovery -InstallRoot $InstallRoot -ConfigPath $configPath -Record $record
                if (-not $match.Matches) {
                    Stop-Setup -Step "step 2 (machine)" -Problem "An install already exists on this computer that does not match the saved enrollment ($($match.Reason)); setup will not overwrite or adopt it." `
                        -Unchanged "the existing install, the saved record, the token and the Scheduled Task, and the enrollment code was not requested" `
                        -Fix "If it is a partial install left by the interrupted setup, run tools\uninstall.ps1 and then setup.ps1 again -- the saved enrollment and the stored token are kept, so no new enrollment code is needed. If it is a different install, leave it alone." -ExitCode 2
                }
                $mode = "ResumeFinish"
            }
            Write-Host "  Found an unfinished setup: installation $($record.InstallationId), enrolled with release $($record.ReleaseVersion)." -ForegroundColor Yellow
            $answer = Read-SetupAnswer -Prompt "Resume it using the saved enrollment (no new code needed)? [Y/n] (Enter = resume)"
            if ($null -ne $answer -and @("n", "no") -contains $answer.Trim().ToLowerInvariant()) {
                Stop-Setup -Step "step 2 (machine)" -Problem "Resuming the unfinished setup was declined." `
                    -Unchanged "everything on this computer, including the saved record and the token" `
                    -Fix "To start over instead: run tools\uninstall.ps1, delete the saved record '$($recovery.Path)', get a NEW enrollment code, then run setup.ps1." -ExitCode 2
            }
            $customerId = $record.CustomerId
            $branchId = $record.BranchId
            $installationId = $record.InstallationId
            Write-Host "  OK: resuming ($(if ($mode -eq 'ResumeFinish') { 'the install is already in place -- only verification remains' } else { 'installing with the saved enrollment' })); no enrollment request will be made." -ForegroundColor Green
        } else {
            if ($null -ne $existing) {
                foreach ($item in $existing.Found) { Write-Host "  Found: $item" -ForegroundColor Yellow }
                Stop-Setup -Step "step 2 (machine)" -Problem "A SortView Collector footprint already exists on this computer (listed above); setup will not overwrite or delete it." `
                    -Unchanged "the existing install, its state and its Scheduled Task, and the enrollment code was not requested" `
                    -Fix "An earlier setup may be unfinished. If the API token was already stored, resume the remaining steps with: tools\finish-install.ps1 (safe to re-run). To start over, run tools\uninstall.ps1 first, then setup.ps1 with a NEW enrollment code." -ExitCode 2
            }
            if ($tokenPresent -and -not $ReplaceExistingToken) {
                Write-Host "  An API token (SORTVIEW_API_TOKEN) is already set on this computer (Machine scope; value not shown)." -ForegroundColor Yellow
                Write-Host "  Setup would replace it with the token issued for this installation. If another SortView component uses it, that component would stop working."
                $answer = Read-SetupAnswer -Prompt "Replace the existing token? [y/N] (Enter = no)"
                if ($null -eq $answer -or @("y", "yes") -notcontains $answer.Trim().ToLowerInvariant()) {
                    Stop-Setup -Step "step 2 (machine)" -Problem "A SORTVIEW_API_TOKEN already exists on this computer and replacing it was not confirmed." `
                        -Unchanged "the existing token, and the enrollment code was not requested" `
                        -Fix "If it is a leftover from an unfinished setup, run setup.ps1 again and answer y (or pass -ReplaceExistingToken). If another SortView component uses it, do not replace it." -ExitCode 2
                }
            }
            Write-Host "  OK: no existing Collector install; token: $(if ($tokenPresent) { 'existing token will be replaced (confirmed)' } else { 'none set' })." -ForegroundColor Green
        }

        # === STEP 3: source files ======================================================
        Write-Host ""
        Write-Host "=== Step 3: Tech Logic source files ===" -ForegroundColor Cyan
        $sources = $null
        if ($mode -eq "ResumeFinish") {
            Write-Host "  Skipped: the installed config already records them (the verification steps below check the files)." -ForegroundColor Green
        } else {
            $sources = Get-SourceSelection -Defaults $defaults -Checkins $CheckinsPath -Rejects $RejectsPath -Acs $AcsPath
            Write-Host "  OK: all three source files exist." -ForegroundColor Green
        }

        # === STEP 4: the API is reachable over HTTPS ====================================
        Write-Host ""
        Write-Host "=== Step 4: connecting to SortView ===" -ForegroundColor Cyan
        $reach = Test-ApiReachable -ApiUrl $resolvedApiUrl
        if (-not $reach.Ok) {
            Stop-Setup -Step "step 4 (connection)" -Problem "Could not reach $resolvedApiUrl over HTTPS ($($reach.Detail)). This is usually a network, proxy, firewall or certificate problem on this computer." `
                -Unchanged "nothing was installed$(if ($mode -eq 'Fresh') { ' and the enrollment code was NOT used' })" `
                -Fix "Check this computer's internet access to that address (and any proxy/TLS-inspection policy) with your IT staff, then run setup.ps1 again$(if ($mode -eq 'Fresh') { ' with the same code' })." -ExitCode 1
        }
        Write-Host "  OK: reached the SortView API." -ForegroundColor Green

        if ($mode -eq "Fresh") {
            # === STEP 5: redeem the enrollment code ====================================
            Write-Host ""
            Write-Host "=== Step 5: enrolling this computer ===" -ForegroundColor Cyan
            $code = $EnrollmentCode
            if ($null -eq $code) { $code = Read-EnrollmentCodeSecure }
            if ($null -eq $code -or $code.Length -eq 0) {
                Stop-Setup -Step "step 5 (enrollment)" -Problem "No enrollment code was entered." `
                    -Unchanged "nothing was installed" -Fix "Run setup.ps1 from an interactive PowerShell window and enter the code when asked." -ExitCode 1
            }
            $script:State.CodeAttempted = $true
            $enrollment = Invoke-Enrollment -ApiUrl $resolvedApiUrl -Code $code -HostName (Get-LocalHostName) -Version $runtimeVersion
            $code = $null
            if ($enrollment.Outcome -ne "Enrolled") {
                $definitelyNotUsed = @("Rejected", "RateLimited", "Unreachable", "Redirected") -contains $enrollment.Outcome
                $fix = switch ($enrollment.Outcome) {
                    "Rejected" { "Ask your SortView administrator for a new enrollment code (each code works once and expires after a short time), then run setup.ps1 again." }
                    "RateLimited" { "Wait a minute, then run setup.ps1 again with the same code." }
                    "Unreachable" { "Check the network/proxy, then run setup.ps1 again with the same code." }
                    "Redirected" { "Contact SortView support; the API address in this bundle may be wrong. Do not use a different address unless SortView tells you to." }
                    default { "Ask your SortView administrator for a new enrollment code and, if this repeats, contact SortView support." }
                }
                if ($definitelyNotUsed) { $script:State.CodeAttempted = $false }
                Stop-Setup -Step "step 5 (enrollment)" -Problem $enrollment.Detail `
                    -Unchanged "nothing was installed and no token was stored" -Fix $fix -ExitCode 1
            }
            $script:State.CodeConsumed = $true
            $customerId = $enrollment.CustomerId
            $branchId = $enrollment.BranchId
            $installationId = $enrollment.InstallationId
            Write-Host "  OK: this computer is enrolled (installation $installationId)." -ForegroundColor Green

            # === STEP 6: store the token, immediately; then save what a resume needs ====
            Write-Host ""
            Write-Host "=== Step 6: storing the API token ===" -ForegroundColor Cyan
            $toolExit = Invoke-SetupTool -Path $setTokenScript -Parameters @{ Token = $enrollment.Token }
            $enrollment.Token = $null
            if ($toolExit -ne 0) {
                Stop-Setup -Step "step 6 (API token)" -Problem "The token could not be stored (exit code $toolExit)." `
                    -Unchanged "nothing was installed" -Fix "Ask your SortView administrator for a new enrollment code, then run setup.ps1 again." -ExitCode 1
            }
            $stored = Get-MachineToken
            $storedHash = if ([string]::IsNullOrWhiteSpace($stored)) { $null } else { Get-Sha256Hex -Text $stored }
            $stored = $null
            if ($null -eq $storedHash -or $storedHash -cne $enrollment.TokenHash) {
                Stop-Setup -Step "step 6 (API token)" -Problem "After storing it, the Machine-scope SORTVIEW_API_TOKEN is not the token that was issued." `
                    -Unchanged "nothing was installed" -Fix "Ask your SortView administrator for a new enrollment code, then run setup.ps1 again." -ExitCode 1
            }
            $script:State.TokenStored = $true
            $enrollment.TokenHash = $null
            Write-Host "  OK: token stored (Machine scope; value never shown)." -ForegroundColor Green

            # The IDs exist only in memory until now, and the code just used can never be used again. So
            # the non-secret details (never the code or the token) are saved, read back and checked
            # BEFORE anything is installed: a later local failure can then resume instead of needing a
            # new code. If that cannot be done, setup stops here -- it does not carry on with a warning.
            $recoveryFailure = $null
            try {
                $recoveryJson = ConvertTo-SetupRecoveryJson -CustomerId $customerId -BranchId $branchId -InstallationId $installationId `
                    -ApiUrl $resolvedApiUrl -ReleaseVersion $runtimeVersion
                $savedPath = Save-SetupRecovery -DataRoot $DataRoot -Json $recoveryJson
                $script:State.RecoveryPath = $savedPath
            } catch {
                $recoveryFailure = "could not be saved ($($_.Exception.Message))"
            }
            if ($null -eq $recoveryFailure) {
                try {
                    $recoveryProblems = @(Confirm-SetupRecovery -DataRoot $DataRoot -CustomerId $customerId -BranchId $branchId `
                            -InstallationId $installationId -ApiUrl $resolvedApiUrl -ReleaseVersion $runtimeVersion)
                    if ($recoveryProblems.Count -gt 0) { $recoveryFailure = "could not be verified ($($recoveryProblems -join '; '))" }
                } catch {
                    $recoveryFailure = "could not be verified ($($_.Exception.Message))"
                }
            }
            if ($null -ne $recoveryFailure) {
                # Nothing on disk may be left to steer a later resume: no record was there before this run.
                try {
                    Remove-SetupRecovery -DataRoot $DataRoot
                } catch {
                    Write-Host "  NOTE: the unverified record could not be removed ($($_.Exception.Message)). Delete '$(Get-SetupRecoveryPath -DataRoot $DataRoot)' before running setup.ps1 again." -ForegroundColor Yellow
                }
                Stop-Setup -Step "step 6 (saving the enrollment details)" -Problem "The non-secret enrollment details $recoveryFailure, so setup will not start installing: without them an interrupted install could not be resumed." `
                    -Unchanged "nothing was installed and no Scheduled Task was created or changed; the API token that was just stored is still stored" `
                    -Fix "Fix what is named above (the folder '$(Join-Path $DataRoot 'setup')' must be writable by Administrators), ask your SortView administrator for a NEW enrollment code, and run setup.ps1 again. It will ask before replacing the token that is already stored (or pass -ReplaceExistingToken)." -ExitCode 1
            }
            $script:State.RecoveryOnDisk = $true
            Write-Host "  OK: the non-secret enrollment details were saved and verified (Administrators/SYSTEM only), so an interrupted setup can resume without a new code." -ForegroundColor Green
        } else {
            Write-Host ""
            Write-Host "=== Steps 5-6: not needed -- resuming with the saved enrollment and the token already stored on this computer ===" -ForegroundColor Cyan
        }

        # === STEP 7: install ===========================================================
        Write-Host ""
        Write-Host "=== Step 7: installing the Collector ===" -ForegroundColor Cyan
        $script:State.InstallStarted = $true
        if ($mode -eq "ResumeFinish") {
            Write-Host "  Skipped: the matching install is already in place." -ForegroundColor Green
        } else {
            $installParameters = @{
                InstallRoot = $InstallRoot; DataRoot = $DataRoot
                CustomerId = $customerId; BranchId = $branchId; InstallationId = $installationId
                ApiUrl = $resolvedApiUrl
                CheckinsPath = $sources["checkins"]; RejectsPath = $sources["rejects"]; AcsPath = $sources["acs"]
                SuppressNextSteps = $true
            }
            $toolExit = Invoke-SetupTool -Path $installScript -Parameters $installParameters
            if ($toolExit -ne 0) {
                Stop-Setup -Step "step 7 (install)" -Problem "install.ps1 did not finish (exit code $toolExit) -- see its message above." `
                    -Unchanged "the Scheduled Task was not created" `
                    -Fix "Fix what the message above names, then run setup.ps1 again: it resumes with the saved enrollment (no new enrollment code is needed). If install.ps1 left a partial install behind, run tools\uninstall.ps1 first -- the saved enrollment and the stored token are kept." -ExitCode $toolExit
            }
        }

        # === STEP 8: preflights, bootstrap, task registration (DISABLED) ================
        Write-Host ""
        Write-Host "=== Step 8: verifying and registering (preflight, bootstrap, Scheduled Task) ===" -ForegroundColor Cyan
        $toolExit = Invoke-SetupTool -Path $finishScript -Parameters @{
            InstallRoot = $InstallRoot; ConfigPath = $configPath; UseExistingMachineToken = $true
        }
        if ($toolExit -ne 0) {
            Stop-Setup -Step "step 8 (verification)" -Problem "The verification steps did not complete (exit code $toolExit) -- see the message above." `
                -Unchanged "the token and the installed files stay in place; the Scheduled Task was not enabled" `
                -Fix "Fix what the message above names, then run setup.ps1 again -- it resumes with the saved enrollment and the stored token (no new enrollment code is needed) -- or run tools\finish-install.ps1 directly." -ExitCode $toolExit
        }

        # The task must exist and be Disabled before anything is offered.
        $task = Get-SortViewTask
        if ($null -eq $task) {
            Stop-Setup -Step "step 8 (verification)" -Problem "The verification finished but the '$($script:TaskName)' Scheduled Task does not exist." `
                -Unchanged "the token and the installed files stay in place" -Fix "Run setup.ps1 again (it resumes) or tools\finish-install.ps1 to register it." -ExitCode 1
        }
        if ([string]$task.State -ne "Disabled") {
            Stop-Setup -Step "step 8 (verification)" -Problem "The Scheduled Task is '$([string]$task.State)', not Disabled, after setup." `
                -Unchanged "setup will now disable it" -Fix "Inspect the task in Task Scheduler; it must be Disabled until you choose to enable it." -ExitCode 2
        }

        # Setup has completed: the saved enrollment has done its job.
        try {
            Remove-SetupRecovery -DataRoot $DataRoot
            $script:State.RecoveryOnDisk = $false
        } catch {
            Write-Host "  Note: the saved (non-secret) enrollment record could not be removed: $($_.Exception.Message)" -ForegroundColor Yellow
        }

        # === STEP 9: optional, explicit enable =========================================
        Write-Host ""
        Write-Host "=== SortView Collector setup COMPLETE. The task is registered DISABLED. ===" -ForegroundColor Green
        Write-Host ""
        Write-Host "  [OK] Release bundle verified against its manifest"
        Write-Host "  [OK] Enrolled with SortView (installation $installationId)$(if ($mode -ne 'Fresh') { ' -- resumed from the saved enrollment' }); token stored (never shown)"
        Write-Host "  [OK] Installed from release $runtimeVersion"
        Write-Host "  [OK] Interactive and SYSTEM-context preflight passed"
        Write-Host "  [OK] Starting cursors seeded for every source"
        Write-Host "  [OK] Scheduled Task '$($script:TaskName)' registered -- State: Disabled"
        Write-Host ""

        $enableNow = [bool]$EnableTask
        if (-not $enableNow) {
            Write-Host "Nothing will upload until the task is enabled. Enabling starts the recurring 15-minute runs."
            $answer = Read-SetupAnswer -Prompt "Enable the scheduled task now? [y/N] (Enter = leave it disabled)"
            $enableNow = ($null -ne $answer -and @("y", "yes") -contains $answer.Trim().ToLowerInvariant())
        }
        if ($enableNow) {
            try {
                Enable-SortViewTask
                $after = Get-SortViewTask
                if ($null -eq $after -or [string]$after.State -eq "Disabled") { throw "the task is still Disabled after enabling" }
                $script:State.TaskEnabledOnPurpose = $true
                Write-Host "  The Scheduled Task is now ENABLED (State: $([string]$after.State)); it will run at its next 15-minute trigger." -ForegroundColor Yellow
                Write-Host "Optional immediate run (otherwise it runs at its next 15-minute trigger):"
                Write-Host "    Start-ScheduledTask -TaskName '$($script:TaskName)'"
                Write-Host "This script did not start a Collector run."
            } catch {
                Stop-Setup -Step "step 9 (enable)" -Problem "Enabling the Scheduled Task failed: $($_.Exception.Message)" `
                    -Unchanged "everything else is complete; the task stays Disabled" -Fix "Enable it yourself when ready:  Enable-ScheduledTask -TaskName '$($script:TaskName)'" -ExitCode 1
            }
        } else {
            Write-Host "The Scheduled Task was left DISABLED. When you are ready:"
            Write-Host "    Enable-ScheduledTask -TaskName '$($script:TaskName)'"
            Write-Host "Optional immediate run after enabling (otherwise it runs at its next 15-minute trigger):"
            Write-Host "    Start-ScheduledTask -TaskName '$($script:TaskName)'"
            Write-Host "This script did not start a Collector run."
        }
        $exitCode = 0
    } catch {
        $stopCode = $null
        if ($_.Exception.Data.Contains("SortViewSetupExitCode")) { $stopCode = [int]$_.Exception.Data["SortViewSetupExitCode"] }
        if ($null -ne $stopCode) {
            $exitCode = $stopCode
        } else {
            # An unexpected error: the type and message only, never any variable's contents.
            Write-Host ""
            Write-Host "SETUP STOPPED: unexpected error: $($_.Exception.GetType().Name): $($_.Exception.Message)" -ForegroundColor Red
            foreach ($line in (Get-EnrollmentStateLines)) { Write-Host "  $line" }
            $exitCode = 1
        }
    } finally {
        # Best-effort scrubbing of secret material held by this run.
        if ($null -ne $enrollment) {
            $enrollment.Token = $null
            $enrollment.TokenHash = $null
        }
        $code = $null
        $enrollment = $null
        # A task this run created must never be left enabled by a run that did not deliberately
        # enable it. (Before InstallStarted a task that exists is not ours -- never touched.)
        if ($script:State.InstallStarted -and -not $script:State.TaskEnabledOnPurpose) {
            try {
                $leftover = Get-SortViewTask
                if ($null -ne $leftover -and [string]$leftover.State -ne "Disabled") {
                    Disable-SortViewTask
                    Write-Host "  The Scheduled Task was found enabled after a failed setup and has been DISABLED." -ForegroundColor Yellow
                }
            } catch {
                Write-Host "  Could not confirm the Scheduled Task is disabled; check it in Task Scheduler." -ForegroundColor Yellow
            }
        }
        [GC]::Collect()
    }
    return $exitCode
}

# Run only when executed as a script. Dot-sourcing (. .\setup.ps1) just defines
# the functions above -- how the tests drive the flow with the side effects
# replaced -- and runs nothing.
if ($MyInvocation.InvocationName -ne ".") {
    $result = @(Invoke-SetupMain @PSBoundParameters)
    exit ([int]$result[-1])
}
