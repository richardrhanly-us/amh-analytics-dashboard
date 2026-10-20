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
      2. Read-only machine checks: a first install needs a CLEAN machine. An
         existing install, config or Scheduled Task -- or, left behind in the
         data root by an earlier install, an API token (api_token.dpapi), a
         Contract v2 secret, a state file of either contract or a
         classification rules file -- is listed and setup stops: none is ever
         adopted, replaced or deleted, and replacing a token is never offered. A
         saved enrollment from an interrupted setup is recognised here (see
         RESUME).
      3. The three Tech Logic source files are located and must EXIST.
      4. The SortView API is reached over HTTPS (nothing is consumed by this),
         and the setup folder <DataRoot>\setup is created, restricted to
         Administrators and SYSTEM, and proved writable.
         Steps 1-4 all happen BEFORE the enrollment code is requested, so an
         avoidable local problem never burns a single-use code.
      5. The enrollment code is read as a hidden SecureString and redeemed with
         POST /collector/enroll (sending the code, this computer's name, the
         bundle's canonical version, and `contract_version: 2`: this release
         enrolls on Contract v2). The response is validated strictly. A code
         issued for the LEGACY contract is refused `contract_not_permitted`
         without being used; setup then sends the legacy request ONCE with the
         same code and follows the legacy steps below unchanged. No other
         refusal, error or status is retried (see REFUSALS).

    CONTRACT V2 (the code was issued for a contract 2 installation). Steps 6-9
    are these instead; see docs/collector-v2.md, "Fresh Contract v2 installation":

      6. FIRST, before anything else is attempted, everything non-secret the
         server just said is saved to <DataRoot>\setup, read back and verified:
         the resume record (the IDs, the API address, the key id, the time zone)
         and the site's classification rules, exactly as the server sent them.
         THEN the token is stored and verified (api_token.dpapi), as below.
      7. install.ps1 is run with the returned IDs and -ContractMode v2 with the
         key id and time zone (a v2 collector_config.json); then the staged
         rules are installed as <DataRoot>\config\classification_rules.json.
      8. tools\finish-install.ps1: the local secret (created ON THIS MACHINE by
         the Collector, never sent, never shown), both v2 preflights, the cursor
         seeded at the end of each source file, the task registered DISABLED.
      9. The Scheduled Task is ENABLED -- setup's last action, with no question,
         and only because every step above succeeded. No run is started.

    LEGACY (the code was issued for a contract 1 installation), unchanged:

      6. The returned token is stored IMMEDIATELY in
         <DataRoot>\secrets\api_token.dpapi (DPAPI, Administrators + SYSTEM
         only) by tools\set-api-token.ps1, which pipes it on STDIN to the
         BUNDLE's own runtime (`api-token set` with the issued customer/branch
         and -DataRoot -- the exact file the installed config will read) --
         before installing anything. The Collector then verifies it
         (`api-token check`). The Machine-scope SORTVIEW_API_TOKEN is neither
         set nor read. The non-secret enrollment details are saved for RESUME,
         read back and checked. Installing does not start until that record
         exists and verifies; if it cannot be saved or verified, setup stops here
         (the token stays stored).
      7. install.ps1 is run with the returned IDs (manifest verification,
         config, directories and every existing safety check stay canonical).
      8. tools\finish-install.ps1 is run with -UseExistingMachineToken (a
         historical name: it means "use the token already stored in
         api_token.dpapi", never an environment variable): the
         interactive preflight, the SYSTEM-context preflight, the starting-
         cursor bootstrap (state must cover every source) and Scheduled Task
         registration -- DISABLED.
      9. Only then, and only on explicit confirmation, may the task be enabled.
         The saved enrollment details are removed once setup has completed.

    THE SCHEDULED TASK STAYS DISABLED until every step above succeeded. For a
    legacy enrollment it is then enabled only on step 9's explicit confirmation
    (or -EnableTask); pressing Enter does not enable it. For a Contract v2
    enrollment it is then enabled by setup itself. On any failure this script
    also disables the task if anything left it enabled. It never starts a
    Collector run.

    REFUSALS. The API answers a VALID code it will not enroll with HTTP 409 and
    one of four fixed codes -- contract_not_permitted, collector_upgrade_required,
    enrollment_not_ready, unsupported_contract_version -- and guarantees none of
    them used the code. Setup reads that code (and nothing else in the body, which
    is never shown) and says so. A rate limit (429) and a redirect never
    reached the API either, and are reported the same way.

    UNKNOWN RESULTS. Any other failure of the enrollment request leaves it
    UNKNOWN whether the server enrolled this computer: no response at all (a
    timeout, a reset, a connection closed after the request was sent), a 5xx,
    an unexpected status, an unrecognized 409, or a 200 that could not be read.
    Setup never says the code "was not used" for these and never advises
    getting a new one -- a Contract v2 enrollment exists exactly once, so a
    new code would be refused. It says: run setup.ps1 again with the SAME
    code; if that code is then refused as invalid or already used, the first
    request may have completed -- contact SortView support. A 400 means only
    that THIS request enrolled nothing; its advice puts that case first. Setup
    never retries by itself (the one legacy fallback above is not a retry of
    an unknown result).

    SECRETS. The enrollment code is entered as a SecureString and converted to
    text only for the one request body, then cleared. The agent token is
    converted to a SecureString the moment the response is validated and the
    plain copy is cleared; it is handed to tools\set-api-token.ps1 as a
    SecureString parameter (in-process -- never a command-line argument). Neither
    value is written to the console, a file, a log or the config. This script
    writes two files, both under <DataRoot> in folders restricted to
    Administrators and SYSTEM: the resume record, which has no field that could
    hold either secret, and -- for Contract v2 -- the site's classification rules
    as the server sent them (staged in <DataRoot>\setup, then installed in
    <DataRoot>\config). The rules hold the site's own account names: they are
    never printed and never in the resume record. The Contract v2 HMAC secret is
    generated by the Collector on this machine; this script never holds it. The
    key id (not a secret) is written to the resume record and the config, and is
    not printed.

    RESUME. Enrollment is single-use, and the code is never made reusable -- so
    a local failure after the code was redeemed must not need a new one. Right
    after redemption the token is stored (api_token.dpapi) and ONLY the non-secret
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

    A CONTRACT V2 ENROLLMENT IS NEVER RECOVERED WITH A NEW CODE. It created the
    branch's one v2 identity on the server, and a second code would be refused. Its
    record (schema_version 2) is saved BEFORE the token and also holds the
    contract, the key id, the time zone and the SHA-256 of the staged rules file;
    a resume completes only what is unfinished, reusing the token, the rules, and
    a valid existing secret and cursor, and refuses if the config or a rules file
    on the machine is not that enrollment's. Three cases need SortView support:
    the server enrolled but its response never arrived; the token could not be
    stored after enrollment (support issues a replacement; setup then resumes);
    the enrolled machine is lost or replaced.

    RE-RUNS WITHOUT A SAVED ENROLLMENT. An existing install (files, config or
    Scheduled Task) or leftover Collector data (see step 2) is refused, never
    overwritten and never deleted: an
    unfinished manual setup is completed with tools\finish-install.ps1 (safe to
    re-run); a live install is updated with tools\update.ps1; starting over needs
    tools\uninstall.ps1 and a NEW enrollment code.

    SOURCES AND URL. The production API URL and the standard Tech Logic file
    locations come from this bundle's collector_config.example.json (the one
    canonical place they are recorded); -ApiUrl and -CheckinsPath/-RejectsPath/
    -AcsPath override them for development, tests and nonstandard installs.

    EVENTS. Off unless a host turns them on. A program that runs this script
    in-process (a graphical installer) may dot-source it, call
    Enable-SetupEvents, then Invoke-SetupMain, and read records tagged
    SortView.Setup.Event (schema sortview.setup.event/1) from the
    Information stream. They describe the same stages, stops and outcome the
    console shows, and never hold the code, the token, a rule, the key id or
    a server response. Running .\setup.ps1 never emits one.

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

.PARAMETER EnableTask
    Legacy (contract 1) enrollments only: after EVERY step succeeded, enable
    the Scheduled Task without asking. Never enables on any failure. A
    Contract v2 enrollment enables the task itself once every step succeeded,
    with or without this switch.

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
    [switch]$EnableTask
)

$ErrorActionPreference = "Stop"

# The one Scheduled Task this Collector uses (tools\register-task.ps1).
$script:TaskName = "SortView Collector"
# The three source names the Collector's parser recognizes -- what install.ps1 writes.
$script:SourceNames = @("checkins", "rejects", "acs")
$script:ExpectedResponseKeys = @("customer_id", "branch_id", "installation_id", "agent_token")
# A Contract v2 enrollment returns those four and exactly these five more (the API's stage-A1 contract).
$script:V2ResponseKeys = $script:ExpectedResponseKeys + @("contract_version", "contract_mode", "key_id", "timezone", "classification_rules")
# The refusals the API may give a caller holding a VALID code (HTTP 409, a fixed `code`). None of them consumes the code.
$script:EnrollmentRefusalCodes = @("collector_upgrade_required", "contract_not_permitted", "unsupported_contract_version", "enrollment_not_ready")
# A server-issued key_id (a lower-case UUIDv4) -- the Collector's own rule (collector/v2_events.py UUID4_PATTERN).
$script:KeyIdPattern = '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
# An IANA time zone NAME is made of these characters; whether it is a real zone is the server's and the Collector's to say.
$script:TimezonePattern = '^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$'
# The five lists of a classification rules document (collector/v2_rules.py), used only to compare SHAPES -- never shown.
$script:RulesListNames = @("destinations", "branch_services_names", "collection_services_names", "branch_services_da_patterns", "collection_services_da_patterns")
# The complete, closed set of fields the resume record may contain. Nothing secret is on it.
$script:RecoveryKeys = @("schema_version", "created_utc", "release_version", "api_url", "customer_id", "branch_id", "installation_id")
# A Contract v2 enrollment's record (schema_version 2) holds those and exactly these: still nothing secret, and no rule
# (`rules_sha256` is the hash of the staged rules FILE, which ties that file to this enrollment without holding a word of it).
$script:RecoveryKeysV2 = $script:RecoveryKeys + @("contract_version", "contract_mode", "key_id", "timezone", "rules_sha256")
# install.ps1 binds these as [Nullable[int]] (Int32); a larger value could not be passed.
$script:MaxInstallerId = [int]::MaxValue
$script:State = $null

# === structured events (optional: OFF unless a host turns them on) ===========================
# A host that runs this script IN-PROCESS -- a graphical installer -- dot-sources it, calls Enable-SetupEvents, and
# then reads records tagged SortView.Setup.Event from the Information stream instead of the console text. Until then
# no event is ever emitted, so an ordinary run's console output (redirected or not) and its exit code are exactly
# what they were. An event holds fixed identifiers, typed values, and text the console already shows; every field is
# on a closed allow-list and anything else -- an object, a SecureString, an unknown field -- drops the whole event.
# Emitting never throws and never decides anything: setup does the same whether or not anyone is listening.

$script:SetupEventSchema = "sortview.setup.event/1"
$script:SetupEventTag = "SortView.Setup.Event"
$script:SetupEventsEnabled = $false
$script:SetupEventSequence = 0
$script:SetupStage = $null
$script:SetupStageIds = @("elevation", "bundle_verification", "bundle", "machine", "sources", "connection", "setup_folder",
    "enrollment", "enrollment_save", "token_store", "install", "rules_install", "verification", "record_cleanup", "task_enable")
# Each event type and, in order, the only fields it may carry.
$script:SetupEventTypes = [ordered]@{
    stage_started         = @()
    stage_completed       = @("outcome")
    stage_skipped         = @("reason")
    mode_detected         = @("mode", "contract")
    enrollment_result     = @("requested_contract", "enrollment_outcome", "refusal_code", "code_used", "contract")
    run_stopped           = @("step_label", "problem", "unchanged", "fix", "advice", "exit_code", "contract",
        "code_attempted", "code_consumed", "code_rejected", "token_stored", "recovery_on_disk")
    run_failed_unexpected = @("exception_type", "message", "advice", "contract",
        "code_attempted", "code_consumed", "code_rejected", "token_stored", "recovery_on_disk")
    run_finished          = @("result", "exit_code", "mode", "contract", "task_state", "release_version", "installation_id")
}
# What each field may hold: a list means exactly one of those values; otherwise a kind. Any field may also be $null.
$script:SetupEventFieldRules = @{
    outcome            = @("ok", "already_done", "enabled", "left_disabled", "warning")
    reason             = @("resume", "already_installed", "not_applicable")
    mode               = @("fresh", "resume_install", "resume_finish")
    contract           = @(1, 2)
    requested_contract = @(1, 2)
    enrollment_outcome = @("Enrolled", "Refused", "Rejected", "RateLimited", "Unreachable", "Redirected", "BadResponse", "ServerError")
    refusal_code       = @($script:EnrollmentRefusalCodes)
    code_used          = @("used", "not_used", "not_used_by_this_request", "unknown")
    result             = @("completed", "stopped", "failed")
    task_state         = @("Enabled", "Disabled")
    step_label         = "text"
    problem            = "text"
    unchanged          = "text"
    fix                = "text"
    message            = "text"
    advice             = "lines"
    exception_type     = "type_name"
    release_version    = "version"
    exit_code          = "int"
    installation_id    = "id"
    code_attempted     = "bool"
    code_consumed      = "bool"
    code_rejected      = "bool"
    token_stored       = "bool"
    recovery_on_disk   = "bool"
}
$script:SetupEventMaxText = 4000
$script:SetupEventMaxLines = 20

function Enable-SetupEvents {
    # For a host only (see above). Nothing in this script calls it.
    $script:SetupEventsEnabled = $true
}

function Test-SetupEventValue {
    # PURE. Whether $Value is allowed by $Rule (an entry of $script:SetupEventFieldRules). $null always is.
    param($Rule, $Value)
    if ($null -eq $Value) { return $true }
    if ($Rule -is [array]) {
        foreach ($choice in $Rule) {
            if ($Value.GetType() -eq $choice.GetType() -and $Value -ceq $choice) { return $true }
        }
        return $false
    }
    switch ($Rule) {
        "text" { return ($Value -is [string] -and $Value.Length -le $script:SetupEventMaxText) }
        "lines" {
            if ($Value -isnot [array] -or $Value.Count -gt $script:SetupEventMaxLines) { return $false }
            foreach ($line in $Value) {
                if ($line -isnot [string] -or $line.Length -gt $script:SetupEventMaxText) { return $false }
            }
            return $true
        }
        "int" { return ($Value -is [int]) }
        "id" { return ($Value -is [int] -and $Value -ge 1) }
        "bool" { return ($Value -is [bool]) }
        "version" { return ($Value -is [string] -and $Value.Length -le 64 -and $Value -cmatch '^[0-9A-Za-z][0-9A-Za-z.+\-]*$') }
        "type_name" { return ($Value -is [string] -and $Value.Length -le 256 -and $Value -cmatch '^[A-Za-z0-9_.`+]+$') }
    }
    return $false
}

function ConvertTo-SetupEventRecord {
    # PURE. The record for one event, or $null when anything about it is not allowed: an unknown type, stage or field,
    # or a value of the wrong kind. Every field of the type is present, in a fixed order ($null when not given), so a
    # reader sees exactly one shape per type.
    param([string]$Type, $Fields, [int]$Sequence, $Stage)
    if (-not $script:SetupEventTypes.Contains($Type)) { return $null }
    if ($null -ne $Stage -and ($Stage -isnot [string] -or $script:SetupStageIds -cnotcontains $Stage)) { return $null }
    if ($null -eq $Fields) { $Fields = @{} }
    if ($Fields -isnot [hashtable]) { return $null }
    $allowed = @($script:SetupEventTypes[$Type])
    foreach ($name in $Fields.Keys) {
        if ($name -isnot [string] -or $allowed -cnotcontains $name) { return $null }
    }
    $record = [ordered]@{ schema = $script:SetupEventSchema; seq = $Sequence; type = $Type; stage = $Stage }
    foreach ($name in $allowed) {
        $value = $Fields[$name]
        if (-not (Test-SetupEventValue -Rule $script:SetupEventFieldRules[$name] -Value $value)) { return $null }
        $record[$name] = $value
    }
    return [pscustomobject]$record
}

function Write-SetupEvent {
    # The ONE place an event leaves setup, and only after Enable-SetupEvents: a record tagged SortView.Setup.Event on
    # the Information stream, never displayed (-InformationAction SilentlyContinue, whatever the preference). A record
    # that is not allowed is dropped; its sequence number is still used, so a gap shows that something was dropped.
    # Never throws.
    param([string]$Type, $Fields)
    if (-not $script:SetupEventsEnabled) { return }
    try {
        $script:SetupEventSequence++
        $record = ConvertTo-SetupEventRecord -Type $Type -Fields $Fields -Sequence $script:SetupEventSequence -Stage $script:SetupStage
        if ($null -ne $record) {
            Write-Information -MessageData $record -Tags $script:SetupEventTag -InformationAction SilentlyContinue
        }
    } catch {
        $null = $_
    }
}

function Enter-SetupStage {
    # A stage is starting. Also names the stage any stop is reported against (Stop-Setup), events or not.
    param([string]$Id)
    $script:SetupStage = $Id
    Write-SetupEvent -Type "stage_started"
}

function Complete-SetupStage {
    param([string]$Outcome = "ok")
    Write-SetupEvent -Type "stage_completed" -Fields @{ outcome = $Outcome }
}

function Skip-SetupStage {
    # A stage that did not run in this invocation, and why -- never reported as completed.
    param([string]$Id, [string]$Reason)
    $script:SetupStage = $Id
    Write-SetupEvent -Type "stage_skipped" -Fields @{ reason = $Reason }
}

function Get-SetupEventRunState {
    # The facts every stop already states in words (Get-EnrollmentStateLines), as values. The contract only once it is
    # actually known: after the server answered, or from a saved enrollment. Never throws.
    try {
        $state = $script:State
        return @{
            contract         = $(if ($state.CodeConsumed) { [int]$state.Contract } else { $null })
            code_attempted   = [bool]$state.CodeAttempted
            code_consumed    = [bool]$state.CodeConsumed
            code_rejected    = [bool]$state.CodeRejected
            token_stored     = [bool]$state.TokenStored
            recovery_on_disk = [bool]$state.RecoveryOnDisk
        }
    } catch {
        return @{}
    }
}

function Write-SetupEnrollmentResult {
    # One enrollment request's result as fixed values: never the code, the request, the response or the token.
    # code_used follows exactly the classification the stop advice uses (see the enrollment step).
    param([int]$RequestedContract, [string]$Outcome, $RefusalCode, $Contract)
    $used = "unknown"
    if ($Outcome -eq "Enrolled") { $used = "used" }
    elseif (@("Refused", "RateLimited", "Redirected") -contains $Outcome) { $used = "not_used" }
    elseif ($Outcome -eq "Rejected") { $used = "not_used_by_this_request" }
    Write-SetupEvent -Type "enrollment_result" -Fields @{
        requested_contract = $RequestedContract; enrollment_outcome = $Outcome; refusal_code = $RefusalCode
        code_used = $used; contract = $Contract
    }
}

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

function Test-ApiTokenStored {
    # Whether <DataRoot>\secrets\api_token.dpapi exists -- never its content. (The Collector keeps its
    # token there and only there, DPAPI-protected, Administrators + SYSTEM only; this elevated script may look.)
    param([string]$DataRoot)
    return (Test-Path -LiteralPath (Join-Path $DataRoot "secrets\api_token.dpapi"))
}

function Invoke-ApiTokenCheck {
    # The Collector's own verification of the stored token -- DPAPI, ACL and customer/branch binding --
    # run with the bundle's runtime. Returns its exit code (0 = stored and usable). The token itself never
    # comes back into this script.
    param([string]$ExePath, [int]$CustomerId, [int]$BranchId, [string]$DataRoot)
    $global:LASTEXITCODE = $null
    & $ExePath api-token check --customer-id $CustomerId --branch-id $BranchId --data-root $DataRoot | Out-Host
    return [int]$global:LASTEXITCODE
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

function Get-ResponseBodyText {
    # A successful response's body, decoded as UTF-8 from its raw bytes. (Windows PowerShell 5.1 decodes
    # `application/json` with no charset as ISO-8859-1, which would corrupt any non-ASCII character in the
    # classification rules; the API sends UTF-8.) Never logged.
    param($Response)
    try {
        $stream = $Response.RawContentStream
        if ($null -ne $stream) { return [System.Text.Encoding]::UTF8.GetString($stream.ToArray()) }
    } catch {
        $null = $_
    }
    return [string]$Response.Content
}

function Get-ErrorBodyText {
    # The body of an HTTP error response, or $null. Read ONLY for a 409 (see Invoke-SetupHttp), bounded,
    # and used only to pick out one fixed refusal code -- it is never shown or kept.
    param($ErrorRecord)
    $text = $null
    try {
        if ($null -ne $ErrorRecord.ErrorDetails -and -not [string]::IsNullOrEmpty($ErrorRecord.ErrorDetails.Message)) {
            $text = [string]$ErrorRecord.ErrorDetails.Message
        }
    } catch {
        $text = $null
    }
    if ($null -eq $text) {
        try {
            $stream = $ErrorRecord.Exception.Response.GetResponseStream()
            if ($stream.CanSeek) { $stream.Position = 0 }
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
            $text = $reader.ReadToEnd()
        } catch {
            $text = $null
        }
    }
    if ($null -ne $text -and $text.Length -gt 4096) { return $null }
    return $text
}

function Invoke-SetupHttp {
    # One HTTPS request. Never throws: returns StatusCode (0 = none), Content
    # (the response body text, or $null) and Failure ($null, or a short
    # non-secret reason). Redirects are NOT followed -- a redirect would carry
    # the request body (the code) to wherever it points. Never logs a body.
    # An error response's body is discarded, with one exception: a 409 (the
    # API's fixed enrollment refusals), whose body is returned for the caller
    # to read a refusal code from and nothing else.
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
        $result.Content = Get-ResponseBodyText -Response $response
    } catch {
        $response = $_.Exception.Response
        if ($null -ne $response) {
            $result.StatusCode = [int]$response.StatusCode
            $result.Failure = "HTTP $($result.StatusCode)"
            if ($result.StatusCode -eq 409) { $result.Content = Get-ErrorBodyText -ErrorRecord $_ }
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
    #
    # -RequestedContract 2 (a Contract v2 enrollment was asked for) expects the
    # OTHER exact shape instead -- those four and contract_version (2),
    # contract_mode ("v2"), key_id (a lower-case UUID), timezone (an IANA-shaped
    # name) and classification_rules (a JSON object) -- and nothing else. The
    # two shapes are never mixed: a four-field answer to a v2 request, or a
    # nine-field answer to a legacy one, is rejected. No problem text ever
    # repeats a value.
    param([string]$Content, [int]$RequestedContract = 1)
    $fail = { param($reason) return [pscustomobject]@{ Valid = $false; Problem = $reason; CustomerId = $null; BranchId = $null; InstallationId = $null; AgentToken = $null; Contract = $null; KeyId = $null; Timezone = $null; Rules = $null } }
    $expectedKeys = if ($RequestedContract -eq 2) { $script:V2ResponseKeys } else { $script:ExpectedResponseKeys }

    if ([string]::IsNullOrWhiteSpace($Content)) { return (& $fail "the response body is empty") }
    $doc = $null
    try {
        $doc = $Content | ConvertFrom-Json
    } catch {
        return (& $fail "the response body is not valid JSON")
    }
    if ($doc -isnot [System.Management.Automation.PSCustomObject]) { return (& $fail "the response body is not a JSON object") }

    $names = @($doc.PSObject.Properties | ForEach-Object { $_.Name })
    foreach ($required in $expectedKeys) {
        if (-not ($names -ccontains $required)) { return (& $fail "the response is missing '$required'") }
    }
    if ($names.Count -ne $expectedKeys.Count) { return (& $fail "the response has unexpected fields") }

    $ids = @{}
    foreach ($name in @("customer_id", "branch_id", "installation_id")) {
        $value = $doc.PSObject.Properties[$name].Value
        if (($value -isnot [int] -and $value -isnot [long]) -or $value -lt 1) { return (& $fail "'$name' is not a positive integer") }
        if ($value -gt $script:MaxInstallerId) { return (& $fail "'$name' is larger than the installer accepts") }
        $ids[$name] = [int]$value
    }

    $token = $doc.PSObject.Properties["agent_token"].Value
    if ($token -isnot [string] -or $token -notmatch '^[A-Za-z0-9_\-]{20,256}$') { return (& $fail "'agent_token' is not a plausible token") }

    $contract = 1
    $keyId = $null
    $timezone = $null
    $rules = $null
    if ($RequestedContract -eq 2) {
        $version = $doc.PSObject.Properties["contract_version"].Value
        if (($version -isnot [int] -and $version -isnot [long]) -or $version -ne 2) { return (& $fail "'contract_version' is not 2") }
        $modeValue = $doc.PSObject.Properties["contract_mode"].Value
        if ($modeValue -isnot [string] -or $modeValue -cne "v2") { return (& $fail "'contract_mode' is not `"v2`"") }
        $keyId = $doc.PSObject.Properties["key_id"].Value
        if ($keyId -isnot [string] -or $keyId -cnotmatch $script:KeyIdPattern) { return (& $fail "'key_id' is not a server-issued key id") }
        $timezone = $doc.PSObject.Properties["timezone"].Value
        if ($timezone -isnot [string] -or $timezone.Length -gt 64 -or $timezone -cnotmatch $script:TimezonePattern) {
            return (& $fail "'timezone' is not a time zone name")
        }
        $rules = $doc.PSObject.Properties["classification_rules"].Value
        if ($rules -isnot [System.Management.Automation.PSCustomObject]) { return (& $fail "'classification_rules' is not a JSON object") }
        $contract = 2
    }

    return [pscustomobject]@{
        Valid = $true; Problem = $null
        CustomerId = $ids["customer_id"]; BranchId = $ids["branch_id"]; InstallationId = $ids["installation_id"]
        AgentToken = $token
        Contract = $contract; KeyId = $keyId; Timezone = $timezone; Rules = $rules
    }
}

function Get-EnrollmentRefusalCode {
    # The fixed refusal code of a 409 from /collector/enroll, or $null. The body must be a JSON object
    # whose `code` is EXACTLY one of the four the API defines; anything else is not a known refusal and is
    # treated conservatively by the caller. Nothing else in the body is read, and none of it is shown.
    param([string]$Content)
    if ([string]::IsNullOrWhiteSpace($Content)) { return $null }
    $doc = $null
    try {
        $doc = $Content | ConvertFrom-Json
    } catch {
        return $null
    }
    if ($doc -isnot [System.Management.Automation.PSCustomObject]) { return $null }
    $property = $doc.PSObject.Properties["code"]
    if ($null -eq $property -or $property.Value -isnot [string]) { return $null }
    foreach ($known in $script:EnrollmentRefusalCodes) {
        if ($property.Value -ceq $known) { return $known }
    }
    return $null
}

function Get-LeftoverArtifacts {
    # Collector DATA left on this machine by an earlier install, which a first install must never adopt
    # or overwrite: a Contract v2 secret (bound to another key), a cursor of either contract, or a
    # classification rules file. tools\uninstall.ps1 keeps the data root by default, so these outlive an
    # uninstall. Reads only (Test-Path); returns what was found, empty for a clean machine.
    param([string]$DataRoot)
    $candidates = [ordered]@{
        "secrets\v2_key.dpapi"               = "a Contract v2 secret"
        "data\state.json"                    = "a Collector state file (the legacy cursor)"
        "data\v2\state_v2.json"              = "a Contract v2 state file (cursor)"
        "config\classification_rules.json"   = "a classification rules file"
        "setup\classification_rules.json"    = "classification rules staged by an earlier setup"
    }
    $found = @()
    foreach ($relative in $candidates.Keys) {
        $path = Join-Path $DataRoot $relative
        if (Test-Path -LiteralPath $path) { $found += "$($candidates[$relative]) at '$path'" }
    }
    return $found
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
    #
    # A Contract v2 enrollment (-ContractVersion 2) adds what a resume needs to finish without the server:
    # the contract, the non-secret key id, the site's time zone, and the SHA-256 of the staged rules file.
    # The rules themselves are never in the record.
    param([int]$CustomerId, [int]$BranchId, [int]$InstallationId, [string]$ApiUrl, [string]$ReleaseVersion,
        [int]$ContractVersion = 1, [string]$KeyId, [string]$Timezone, [string]$RulesSha256)
    $record = [ordered]@{
        schema_version  = 1
        created_utc     = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        release_version = $ReleaseVersion
        api_url         = $ApiUrl
        customer_id     = $CustomerId
        branch_id       = $BranchId
        installation_id = $InstallationId
    }
    if ($ContractVersion -eq 2) {
        $record["schema_version"] = 2
        $record["contract_version"] = 2
        $record["contract_mode"] = "v2"
        $record["key_id"] = $KeyId
        $record["timezone"] = $Timezone
        $record["rules_sha256"] = $RulesSha256
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
    # schema_version 2 is a Contract v2 enrollment's record: the same fields and exactly five more. Either
    # way the field set is closed -- a record is never a mix of the two.
    $schema = $doc.schema_version
    $isV2 = (($schema -is [int] -or $schema -is [long]) -and $schema -eq 2)
    $allowedKeys = if ($isV2) { $script:RecoveryKeysV2 } else { $script:RecoveryKeys }
    foreach ($key in $allowedKeys) {
        if (-not ($names -ccontains $key)) { $problems += "'$key' is missing" }
    }
    if ($names.Count -ne $allowedKeys.Count) { $problems += "it has unexpected fields (only these are allowed: $($allowedKeys -join ', '))" }
    if ($problems.Count -gt 0) { return (& $fail $problems) }

    if (($schema -isnot [int] -and $schema -isnot [long]) -or ($schema -ne 1 -and $schema -ne 2)) { $problems += "'schema_version' must be 1 or 2" }
    if ($isV2) {
        $version = $doc.contract_version
        if (($version -isnot [int] -and $version -isnot [long]) -or $version -ne 2) { $problems += "'contract_version' must be 2" }
        if ($doc.contract_mode -isnot [string] -or $doc.contract_mode -cne "v2") { $problems += "'contract_mode' must be `"v2`"" }
        if ($doc.key_id -isnot [string] -or $doc.key_id -cnotmatch $script:KeyIdPattern) { $problems += "'key_id' is not a key id" }
        if ($doc.timezone -isnot [string] -or $doc.timezone.Length -gt 64 -or $doc.timezone -cnotmatch $script:TimezonePattern) { $problems += "'timezone' is not a time zone name" }
        if ($doc.rules_sha256 -isnot [string] -or $doc.rules_sha256 -cnotmatch '^[0-9a-f]{64}$') { $problems += "'rules_sha256' is not a SHA-256 hash" }
    }
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
            Contract = $(if ($isV2) { 2 } else { 1 })
            KeyId = $(if ($isV2) { [string]$doc.key_id } else { $null })
            Timezone = $(if ($isV2) { [string]$doc.timezone } else { $null })
            RulesSha256 = $(if ($isV2) { [string]$doc.rules_sha256 } else { $null })
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
    param([string]$DataRoot, [int]$CustomerId, [int]$BranchId, [int]$InstallationId, [string]$ApiUrl, [string]$ReleaseVersion,
        [int]$ContractVersion = 1, [string]$KeyId, [string]$Timezone, [string]$RulesSha256)
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
    if ($saved.Contract -ne $ContractVersion) { $problems += "the saved contract differs from the one enrolled" }
    if ($ContractVersion -eq 2) {
        if ($saved.KeyId -cne $KeyId) { $problems += "the saved key id differs from the one issued" }
        if ($saved.Timezone -cne $Timezone) { $problems += "the saved time zone differs from the one issued" }
        if ($saved.RulesSha256 -cne $RulesSha256) { $problems += "the saved rules hash differs from the staged rules" }
    }
    return $problems
}

function Remove-SetupRecovery {
    # Deletes exactly the record file and then its folder if (and only if) the folder
    # is empty -- never recursive, never anything else.
    param([string]$DataRoot)
    # (A Contract v2 enrollment's staged rules file sits beside the record and goes with it.)
    $path = Get-SetupRecoveryPath -DataRoot $DataRoot
    foreach ($file in @($path, (Get-StagedRulesPath -DataRoot $DataRoot))) {
        if (Test-Path -LiteralPath $file -PathType Leaf) { [System.IO.File]::Delete($file) }
    }
    $directory = Split-Path -Parent $path
    if ((Test-Path -LiteralPath $directory -PathType Container) -and @(Get-ChildItem -LiteralPath $directory -Force).Count -eq 0) {
        [System.IO.Directory]::Delete($directory)
    }
}

# === Contract v2: the setup folder, and the site's classification rules ========================
#
# The rules document comes from the SortView API in the enrollment response and from nowhere else: nothing
# is merged into it, no default is added, and no rule is written by hand here. It holds the site's own
# account names, so it exists only in this process's memory and in two Administrators + SYSTEM only
# folders (<DataRoot>\setup while setup is unfinished, then <DataRoot>\config) -- it is never printed,
# and is not in the resume record (only its file's SHA-256 is).

function Get-StagedRulesPath {
    param([string]$DataRoot)
    return (Join-Path (Join-Path $DataRoot "setup") "classification_rules.json")
}

function Get-LiveRulesPath {
    # Where the Collector reads its rules when the config does not say otherwise (collector/v2_config.py).
    param([string]$DataRoot)
    return (Join-Path (Join-Path $DataRoot "config") "classification_rules.json")
}

function Get-FileSha256 {
    # Lower-case hex SHA-256 of a file's bytes.
    param([string]$Path)
    return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash).ToLowerInvariant()
}

function Write-SetupFileAtomically {
    # UTF-8 (no BOM) text into a temporary file in the SAME folder, then a rename into place: a crash leaves
    # no file or the whole file, never part of one. The destination must not already exist. The temporary
    # file is removed if the rename did not happen.
    param([string]$Path, [string]$Text)
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText($temporary, $Text, (New-Object System.Text.UTF8Encoding($false)))
        [System.IO.File]::Move($temporary, $Path)
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) { [System.IO.File]::Delete($temporary) }
    }
}

function Initialize-SetupStaging {
    # BEFORE the enrollment code is requested: creates <DataRoot>\setup, restricts it to Administrators and
    # SYSTEM, and proves a file can be written into it and removed -- so the one thing that must not fail
    # after the code is used (saving what a resume needs) has already been shown to work. Nothing secret is
    # ever put in this folder. Throws on any failure.
    param([string]$DataRoot)
    $directory = Join-Path $DataRoot "setup"
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    Protect-SetupRecoveryDirectory -Path $directory
    $probe = Join-Path $directory "write-probe-$([guid]::NewGuid().ToString('N')).txt"
    Write-SetupFileAtomically -Path $probe -Text "probe"
    if (-not (Test-Path -LiteralPath $probe -PathType Leaf)) { throw "a file written to '$directory' was not there afterwards" }
    [System.IO.File]::Delete($probe)
    return $directory
}

function ConvertTo-ClassificationRulesJson {
    # The server's rules object as JSON text, with enough depth that nothing is cut off (the default depth
    # would flatten the destinations list). Compressed form is used only for comparisons.
    param($Rules, [switch]$Compress)
    if ($Compress) { return (ConvertTo-Json -InputObject $Rules -Depth 20 -Compress) }
    return (ConvertTo-Json -InputObject $Rules -Depth 20)
}

function Get-ClassificationRulesShape {
    # A description of a rules document's STRUCTURE only -- its field names, its schema_version and how
    # many entries each list has -- so two documents can be compared, and a problem reported, without a
    # single name or pattern ever being printed. $null if it is not an object.
    param($Rules)
    if ($Rules -isnot [System.Management.Automation.PSCustomObject]) { return $null }
    $parts = @("fields=" + ((@($Rules.PSObject.Properties | ForEach-Object { $_.Name }) | Sort-Object) -join ","))
    $schema = $Rules.PSObject.Properties["schema_version"]
    $parts += "schema_version=" + $(if ($null -ne $schema) { [string]$schema.Value } else { "absent" })
    foreach ($name in $script:RulesListNames) {
        $property = $Rules.PSObject.Properties[$name]
        if ($null -eq $property) { $parts += "$name=absent" }
        elseif ($property.Value -is [array]) { $parts += "$name=$(@($property.Value).Count)" }
        else { $parts += "$name=not-a-list" }
    }
    return ($parts -join "; ")
}

function Save-StagedRules {
    # Writes the server's rules document into the (already restricted) setup folder, atomically, then reads
    # the file back and proves it is the same document: same structure, same list sizes, same values. Returns
    # the file's SHA-256. Throws on any difference -- with the SHAPES in the message, never the content.
    param([string]$DataRoot, $Rules)
    $path = Get-StagedRulesPath -DataRoot $DataRoot
    if (Test-Path -LiteralPath $path) { throw "staged classification rules already exist at '$path'" }
    Write-SetupFileAtomically -Path $path -Text (ConvertTo-ClassificationRulesJson -Rules $Rules)

    # A parser's own error text can quote the document it choked on, so it is never passed on: a fixed message instead.
    $readBack = $null
    $unreadable = $false
    try {
        $readBack = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch {
        $unreadable = $true
    }
    if ($unreadable) { throw "the staged classification rules could not be read back as JSON" }
    $expectedShape = Get-ClassificationRulesShape -Rules $Rules
    $actualShape = Get-ClassificationRulesShape -Rules $readBack
    if ($null -eq $actualShape -or $actualShape -cne $expectedShape) {
        throw "the staged classification rules do not have the structure the server sent (expected: $expectedShape; found: $actualShape)"
    }
    if ((ConvertTo-ClassificationRulesJson -Rules $readBack -Compress) -cne (ConvertTo-ClassificationRulesJson -Rules $Rules -Compress)) {
        throw "the staged classification rules do not read back as the document the server sent"
    }
    return (Get-FileSha256 -Path $path)
}

function Get-RulesResumeState {
    # For a RESUMED Contract v2 setup: are the rules of the saved enrollment on this machine? The record
    # holds the SHA-256 of the rules file it was saved with; a rules file with any other content is not
    # this enrollment's and is never used or replaced.
    #   Live          -> installed in the config folder, and it is this enrollment's
    #   Staged        -> still in the setup folder (and it is this enrollment's): will be installed
    #   Contradiction -> a rules file is there that is NOT this enrollment's, or there is none at all
    param([string]$DataRoot, [string]$ExpectedSha256)
    $result = { param($state, $reason) return [pscustomobject]@{ State = $state; Reason = $reason } }
    $live = Get-LiveRulesPath -DataRoot $DataRoot
    $staged = Get-StagedRulesPath -DataRoot $DataRoot
    if (Test-Path -LiteralPath $live) {
        if (-not (Test-Path -LiteralPath $live -PathType Leaf)) { return (& $result "Contradiction" "'$live' is not a file") }
        if ((Get-FileSha256 -Path $live) -cne $ExpectedSha256) { return (& $result "Contradiction" "the installed classification rules are not the ones this enrollment was saved with") }
        return (& $result "Live" "the installed classification rules belong to the saved enrollment")
    }
    if (-not (Test-Path -LiteralPath $staged -PathType Leaf)) { return (& $result "Contradiction" "the classification rules saved with this enrollment are missing") }
    if ((Get-FileSha256 -Path $staged) -cne $ExpectedSha256) { return (& $result "Contradiction" "the staged classification rules are not the ones this enrollment was saved with") }
    return (& $result "Staged" "the classification rules are staged and belong to the saved enrollment")
}

function Install-ClassificationRules {
    # Puts this enrollment's staged rules where the Collector reads them -- a copy into a temporary file in
    # the config folder (which install.ps1 has created and restricted), then a rename -- and proves the
    # installed file is byte-for-byte the staged one. A rules file that is already there is kept if it is
    # this enrollment's and refused if it is not; it is never overwritten. Returns the state it ended in
    # (Live), or throws.
    param([string]$DataRoot, [string]$ExpectedSha256)
    $state = Get-RulesResumeState -DataRoot $DataRoot -ExpectedSha256 $ExpectedSha256
    if ($state.State -eq "Contradiction") { throw $state.Reason }
    if ($state.State -eq "Live") { return "AlreadyInstalled" }

    $live = Get-LiveRulesPath -DataRoot $DataRoot
    $temporary = "$live.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::Copy((Get-StagedRulesPath -DataRoot $DataRoot), $temporary)
        [System.IO.File]::Move($temporary, $live)
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) { [System.IO.File]::Delete($temporary) }
    }
    if ((Get-FileSha256 -Path $live) -cne $ExpectedSha256) { throw "the installed classification rules are not the staged ones" }
    return "Installed"
}

function Test-RecoveryLooksContractV2 {
    # For a record that could NOT be validated: does anything say it belongs to a Contract v2 enrollment? Either the
    # rules such an enrollment stages are beside it, or its text names a field only a v2 record has. Reads only, never
    # throws, and decides nothing but which advice is given.
    param([string]$DataRoot)
    if (Test-Path -LiteralPath (Get-StagedRulesPath -DataRoot $DataRoot)) { return $true }
    try {
        $content = Get-Content -LiteralPath (Get-SetupRecoveryPath -DataRoot $DataRoot) -Raw -Encoding UTF8 -ErrorAction Stop
    } catch {
        return $false
    }
    return ([string]$content -match 'contract_version|contract_mode|key_id|rules_sha256|"schema_version"\s*:\s*2')
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
    # The contract too: a Contract v2 enrollment's config selects v2 with the SAME key id and time zone, and
    # a legacy enrollment's config does not select v2 at all.
    $modeProperty = $config.PSObject.Properties["contract_mode"]
    $configIsV2 = ($null -ne $modeProperty -and $modeProperty.Value -is [string] -and $modeProperty.Value -ceq "v2")
    if ($Record.Contract -eq 2) {
        if (-not $configIsV2) { return (& $result $false "collector_config.json does not select Contract v2") }
        $section = $config.PSObject.Properties["v2"]
        if ($null -eq $section -or $section.Value -isnot [System.Management.Automation.PSCustomObject]) { return (& $result $false "collector_config.json has no v2 section") }
        if ([string]$section.Value.key_id -cne $Record.KeyId) { return (& $result $false "collector_config.json has a different v2 key id") }
        if ([string]$section.Value.timezone -cne $Record.Timezone) { return (& $result $false "collector_config.json has a different v2 time zone") }
    } elseif ($configIsV2) {
        return (& $result $false "collector_config.json selects Contract v2 but the saved enrollment is a legacy one")
    }
    return (& $result $true "the installed config matches the saved enrollment")
}

function Get-EnrollmentStateLines {
    # What the operator needs to know about the single-use code, the token and the saved
    # enrollment, given how far this run got. Shown on every stop.
    $lines = @()
    if ($null -ne $script:State -and $script:State.CodeConsumed -and $script:State.Contract -eq 2) {
        # Contract v2: the enrollment also created this branch's v2 identity on the server (its ingest key and its
        # cutover), which exists exactly once. A NEW enrollment code would be refused, so it is never the advice.
        $lines += "The one-time enrollment code HAS BEEN USED and cannot be used again."
        $lines += "Do NOT ask for another enrollment code: this installation's Contract v2 identity already exists on the server, and a new code would be refused."
        if ($script:State.RecoveryOnDisk -and $script:State.TokenStored) {
            $lines += "The permanent API token IS stored on this machine (api_token.dpapi; never shown)."
            $lines += "The non-secret enrollment details and the classification rules are saved under '$(Split-Path -Parent $script:State.RecoveryPath)': run setup.ps1 again to RESUME -- no enrollment code is needed."
        } elseif ($script:State.RecoveryOnDisk) {
            $lines += "The non-secret enrollment details and the classification rules ARE saved, but the permanent API token is NOT stored on this machine."
            $lines += "Contact SortView support: a replacement API token must be issued for this installation. Setup can then resume without a new code."
        } else {
            $lines += "The enrollment details could NOT be saved on this machine, so this setup cannot be resumed here."
            $lines += "Contact SortView support and give them the installation number shown above."
        }
    } elseif ($null -ne $script:State -and $script:State.CodeConsumed) {
        $lines += "The one-time enrollment code HAS BEEN USED and cannot be used again."
        if ($script:State.TokenStored) {
            $lines += "The permanent API token IS stored on this machine (api_token.dpapi; never shown)."
            if ($script:State.RecoveryOnDisk) {
                $lines += "The non-secret enrollment details are saved at '$($script:State.RecoveryPath)': run setup.ps1 again to RESUME -- no new enrollment code is needed."
            } else {
                $lines += "The enrollment details could NOT be saved, so this setup cannot be resumed: ask your SortView administrator for a NEW enrollment code."
            }
        } else {
            $lines += "The permanent API token was NOT stored. Ask your SortView administrator for a NEW enrollment code."
        }
    } elseif ($null -ne $script:State -and $script:State.CodeAttempted) {
        # No answer, or an answer that does not say what the server did: it may have enrolled this computer before
        # the answer was lost. A Contract v2 enrollment exists once, so a NEW code is never the next step here.
        $lines += "The enrollment code may or may not have been used: the result of the enrollment request is UNKNOWN."
        $lines += "Do NOT ask for a new enrollment code yet. Run setup.ps1 again with the SAME code."
        $lines += "If the same code is then refused as invalid or already used, the first request may have completed: contact SortView support."
    } elseif ($null -ne $script:State -and $script:State.CodeRejected) {
        # A 400: this request enrolled nothing -- but that says nothing about an EARLIER request with the same code.
        $lines += "SortView did not accept the enrollment code; this request enrolled nothing."
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
    if ($script:SetupEventsEnabled) {
        # Exactly what was just printed, as values -- for a host, against the stage that was running. Building it can
        # never replace the stop below with a different error.
        try {
            $fields = Get-SetupEventRunState
            $fields["step_label"] = $Step
            $fields["problem"] = $Problem
            $fields["unchanged"] = $Unchanged
            $fields["fix"] = $Fix
            $fields["advice"] = @(Get-EnrollmentStateLines)
            $fields["exit_code"] = $ExitCode
            Write-SetupEvent -Type "run_stopped" -Fields $fields
        } catch {
            $null = $_
        }
    }
    $stop = New-Object System.Exception "setup stopped at $Step"
    $stop.Data["SortViewSetupExitCode"] = $ExitCode
    throw $stop
}

# === enrollment ====================================================================

function Invoke-Enrollment {
    # Redeems the code. Returns Outcome (Enrolled / Rejected / RateLimited /
    # Unreachable / Redirected / BadResponse / ServerError), a non-secret Detail,
    # and -- only when Enrolled -- the three IDs and the token as a SecureString.
    # The plain token and the plain code never leave this function.
    #
    # -ContractVersion 2 asks for a Contract v2 enrollment (`contract_version: 2` in the request) and expects the
    # nine-field answer; then KeyId, Timezone and Rules are returned too. -ContractVersion 1 sends NO
    # `contract_version` at all -- the legacy request, byte for byte -- and expects the four-field answer.
    # A 409 carrying one of the API's fixed refusal codes is the outcome Refused (RefusalCode says which): the API
    # guarantees none of them consumed the code. ONE request is made per call; the caller decides about a retry.
    param([string]$ApiUrl, [SecureString]$Code, [string]$HostName, [string]$Version, [int]$ContractVersion = 1)

    $outcome = { param($name, $detail) return [pscustomobject]@{ Outcome = $name; Detail = $detail; RefusalCode = $null; CustomerId = $null; BranchId = $null; InstallationId = $null; Token = $null; Contract = $null; KeyId = $null; Timezone = $null; Rules = $null } }

    $plainCode = ConvertFrom-SecureStringPlain -Secure $Code
    $body = $null
    try {
        $request = [ordered]@{ enrollment_code = $plainCode; hostname = $HostName; collector_version = $Version }
        if ($ContractVersion -eq 2) { $request["contract_version"] = 2 }
        $body = ($request | ConvertTo-Json -Compress)
        $request = $null
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
            $parsed = ConvertTo-EnrollmentResult -Content $http.Content -RequestedContract $ContractVersion
            $http.Content = $null
            if (-not $parsed.Valid) {
                return (& $outcome "BadResponse" "SortView answered, but the response was not valid ($($parsed.Problem)).")
            }
            $secureToken = ConvertTo-SecureString -String $parsed.AgentToken -AsPlainText -Force
            $result = & $outcome "Enrolled" "enrolled"
            $result.CustomerId = $parsed.CustomerId
            $result.BranchId = $parsed.BranchId
            $result.InstallationId = $parsed.InstallationId
            $result.Token = $secureToken
            $result.Contract = $parsed.Contract
            $result.KeyId = $parsed.KeyId
            $result.Timezone = $parsed.Timezone
            $result.Rules = $parsed.Rules
            $parsed.AgentToken = $null
            $parsed = $null
            return $result
        }
        400 { return (& $outcome "Rejected" "The enrollment code was not accepted (invalid, expired, or already used).") }
        409 {
            $refusal = Get-EnrollmentRefusalCode -Content $http.Content
            $http.Content = $null
            if ($null -ne $refusal) {
                $detail = switch ($refusal) {
                    "collector_upgrade_required" { "This enrollment code requires a newer SortView Collector than this release bundle." }
                    "contract_not_permitted" { "This enrollment code does not permit the contract this setup asked for." }
                    "unsupported_contract_version" { "SortView does not support the contract version this setup asked for." }
                    default { "SortView reports that this installation is not ready to be enrolled." }
                }
                $result = & $outcome "Refused" "$detail The enrollment code was NOT used."
                $result.RefusalCode = $refusal
                return $result
            }
        }
        429 { return (& $outcome "RateLimited" "SortView is limiting requests from this computer.") }
    }
    if ($http.StatusCode -ge 300 -and $http.StatusCode -lt 400) {
        return (& $outcome "Redirected" "SortView redirected the request (HTTP $($http.StatusCode)); it was not followed.")
    }
    if ($http.StatusCode -ge 500) {
        return (& $outcome "ServerError" "SortView reported an error (HTTP $($http.StatusCode)).")
    }
    if ($http.StatusCode -eq 0) {
        return (& $outcome "Unreachable" "No response was received from SortView ($($http.Failure)), so the result of the enrollment request is unknown.")
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
        [switch]$EnableTask
    )

    $script:State = @{
        CodeAttempted = $false; CodeConsumed = $false; TokenStored = $false
        RecoveryOnDisk = $false; RecoveryPath = $null
        # 1 = a legacy enrollment, 2 = Contract v2. Known only once the server has answered (or a saved
        # enrollment was read); it decides the advice every stop gives about the enrollment code.
        Contract = 1
        # This run created the (empty) setup folder for a fresh enrollment that then did not happen.
        StagingPrepared = $false
        # The API answered 400: this request enrolled nothing (which says nothing about an earlier request).
        CodeRejected = $false
        # Set just before anything is installed or finished: only from then on can a Scheduled Task be
        # this run's own. Before that, a task that exists is the customer's and is never touched.
        InstallStarted = $false
        TaskEnabledOnPurpose = $false
    }
    $exitCode = 1
    $enrollment = $null
    # Only for the optional events (see "structured events"): numbering starts again with every run.
    $script:SetupEventSequence = 0
    $script:SetupStage = $null
    $runResult = "failed"
    $detectedMode = $null
    try {
        $bundleRoot = $PSScriptRoot

        # === STEP 0: elevation, before anything else ================================
        Enter-SetupStage -Id "elevation"
        if (-not (Test-IsAdministrator)) {
            Stop-Setup -Step "step 0 (checks)" -Problem "This script must be run from an elevated (Administrator) PowerShell session." `
                -Unchanged "nothing was touched" -Fix "Open PowerShell with 'Run as administrator', go to this folder, and run .\setup.ps1 again." -ExitCode 1
        }
        Complete-SetupStage

        Write-Host "=== SortView Collector: guided setup ===" -ForegroundColor Cyan
        Write-Host "You will need the one-time enrollment code from your SortView administrator."
        Write-Host ""

        # === STEP 1: the bundle -- verified FIRST, before anything in it is trusted ===
        Write-Host "=== Step 1: verifying this release bundle ===" -ForegroundColor Cyan
        Enter-SetupStage -Id "bundle_verification"
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
        Complete-SetupStage
        Enter-SetupStage -Id "bundle"

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
        Complete-SetupStage

        # === STEP 2: this machine (read-only) ==========================================
        Write-Host ""
        Write-Host "=== Step 2: checking this machine ===" -ForegroundColor Cyan
        Enter-SetupStage -Id "machine"
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
        $tokenPresent = [bool](Test-ApiTokenStored -DataRoot $DataRoot)
        $mode = "Fresh"
        $customerId = $null
        $branchId = $null
        $installationId = $null
        $configPath = Join-Path $DataRoot "config\collector_config.json"

        if ($recovery.Status -eq "Malformed") {
            foreach ($problem in $recovery.Problems) { Write-Host "  $problem" -ForegroundColor Red }
            # A damaged record of a CONTRACT V2 enrollment is never "stale": that enrollment exists on the server and a
            # new code would be refused. The legacy advice (delete it, get a new code) applies only to a legacy record.
            $malformedFix = "The record holds only non-secret IDs. If you are sure it is stale, delete that one file and run setup.ps1 again (a NEW enrollment code will then be needed); otherwise contact SortView support."
            if (Test-RecoveryLooksContractV2 -DataRoot $DataRoot) {
                $malformedFix = "This is the record of a Contract v2 enrollment, which already exists on the SortView server. Do NOT ask for another enrollment code (it would be refused) and do NOT delete or edit the record or anything beside it. Contact SortView support."
            }
            Stop-Setup -Step "step 2 (machine)" -Problem "A saved enrollment record exists at '$($recovery.Path)' but it is not valid (listed above); setup will not guess." `
                -Unchanged "the record, the installed files, the token and the Scheduled Task, and the enrollment code was not requested" `
                -Fix $malformedFix -ExitCode 2
        }

        if ($recovery.Status -eq "Valid") {
            if (-not $tokenPresent -and $recovery.Record.Contract -eq 2) {
                # A Contract v2 enrollment exists exactly once on the server: a NEW code would be refused, so the
                # record is kept and the only way forward is a replacement token for THIS installation.
                $script:State.CodeConsumed = $true
                $script:State.Contract = 2
                $script:State.RecoveryOnDisk = $true
                $script:State.RecoveryPath = $recovery.Path
                Stop-Setup -Step "step 2 (machine)" -Problem "A saved Contract v2 enrollment was found (installation $($recovery.Record.InstallationId)), but this computer has no API token (api_token.dpapi) to go with it." `
                    -Unchanged "the saved record, the staged classification rules and everything else, and no enrollment request was made" `
                    -Fix "Do NOT ask for another enrollment code (it would be refused) and do NOT delete the saved record. Contact SortView support: they issue a replacement API token for installation $($recovery.Record.InstallationId). Store it with tools\set-api-token.ps1 -CustomerId $($recovery.Record.CustomerId) -BranchId $($recovery.Record.BranchId) -DataRoot `"$DataRoot`" -ExePath `"$runtimeExe`", then run setup.ps1 again to resume." -ExitCode 2
            }
            if (-not $tokenPresent) {
                Stop-Setup -Step "step 2 (machine)" -Problem "A saved enrollment was found (installation $($recovery.Record.InstallationId)), but this computer has no API token (api_token.dpapi) to go with it." `
                    -Unchanged "the saved record and everything else, and the enrollment code was not requested" `
                    -Fix "The token cannot be recovered and the used enrollment code cannot be reused. Ask your SortView administrator for a NEW enrollment code, delete the saved record '$($recovery.Path)' (non-secret IDs only), then run setup.ps1 again." -ExitCode 2
            }
            # From here, this run is a continuation of an earlier one whose code was already redeemed.
            $script:State.CodeConsumed = $true
            $script:State.TokenStored = $true
            $script:State.RecoveryOnDisk = $true
            $script:State.RecoveryPath = $recovery.Path
            $record = $recovery.Record
            $script:State.Contract = $record.Contract

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
            if ($record.Contract -eq 2) {
                # The rules this enrollment was saved with must be here -- staged or already installed -- and be
                # exactly those: a rules file with any other content belongs to something else and is never used.
                $rulesState = Get-RulesResumeState -DataRoot $DataRoot -ExpectedSha256 $record.RulesSha256
                if ($rulesState.State -eq "Contradiction") {
                    Stop-Setup -Step "step 2 (machine)" -Problem "The saved Contract v2 enrollment (installation $($record.InstallationId)) cannot be resumed safely: $($rulesState.Reason)." `
                        -Unchanged "the saved record, every rules file, the token, the installed files and the Scheduled Task, and no enrollment request was made" `
                        -Fix "Do NOT ask for another enrollment code (it would be refused) and do not delete or replace anything. Contact SortView support." -ExitCode 2
                }
            }
            Write-Host "  Found an unfinished setup: installation $($record.InstallationId), enrolled with release $($record.ReleaseVersion)." -ForegroundColor Yellow
            $detectedMode = $(if ($mode -eq "ResumeFinish") { "resume_finish" } else { "resume_install" })
            Write-SetupEvent -Type "mode_detected" -Fields @{ mode = $detectedMode; contract = $script:State.Contract }
            $answer = Read-SetupAnswer -Prompt "Resume it using the saved enrollment (no new code needed)? [Y/n] (Enter = resume)"
            if ($null -ne $answer -and @("n", "no") -contains $answer.Trim().ToLowerInvariant()) {
                Stop-Setup -Step "step 2 (machine)" -Problem "Resuming the unfinished setup was declined." `
                    -Unchanged "everything on this computer, including the saved record and the token" `
                    -Fix $(if ($record.Contract -eq 2) { "Run setup.ps1 again and resume when you are ready. A Contract v2 enrollment cannot be started over with a new enrollment code (it would be refused): if this machine will not be the Collector after all, contact SortView support." } else { "To start over instead: run tools\uninstall.ps1, delete the saved record '$($recovery.Path)', get a NEW enrollment code, then run setup.ps1." }) -ExitCode 2
            }
            $customerId = $record.CustomerId
            $branchId = $record.BranchId
            $installationId = $record.InstallationId
            Write-Host "  OK: resuming ($(if ($mode -eq 'ResumeFinish') { 'the install is already in place -- only verification remains' } else { 'installing with the saved enrollment' })); no enrollment request will be made." -ForegroundColor Green
            Complete-SetupStage
        } else {
            if ($null -ne $existing) {
                foreach ($item in $existing.Found) { Write-Host "  Found: $item" -ForegroundColor Yellow }
                Stop-Setup -Step "step 2 (machine)" -Problem "A SortView Collector footprint already exists on this computer (listed above); setup will not overwrite or delete it." `
                    -Unchanged "the existing install, its state and its Scheduled Task, and the enrollment code was not requested" `
                    -Fix "An earlier setup may be unfinished. If the API token was already stored, resume the remaining steps with: tools\finish-install.ps1 (safe to re-run). To start over, run tools\uninstall.ps1 first, then setup.ps1 with a NEW enrollment code." -ExitCode 2
            }
            # A first install starts from a machine with NO Collector identity or data on it. An API token, a
            # Contract v2 secret, a cursor of either contract or a rules file already here belongs to some other
            # installation (tools\uninstall.ps1 keeps the data root), and none of them is ever adopted, replaced
            # or offered for replacement -- least of all after the single-use code has been spent.
            $leftovers = @(Get-LeftoverArtifacts -DataRoot $DataRoot)
            if ($tokenPresent) { $leftovers = @("an API token at '$DataRoot\secrets\api_token.dpapi' (value not shown)") + $leftovers }
            if ($leftovers.Count -gt 0) {
                foreach ($item in $leftovers) { Write-Host "  Found: $item" -ForegroundColor Yellow }
                Stop-Setup -Step "step 2 (machine)" -Problem "This computer already holds SortView Collector data from an earlier install or setup (listed above), and there is no saved enrollment to resume. Setup will not adopt, replace or delete any of it." `
                    -Unchanged "everything listed above, and the enrollment code was not requested" `
                    -Fix "If this machine ran a Collector before, contact SortView support before installing again. If the earlier install is truly finished with, tools\uninstall.ps1 -PurgeData removes its data root (this cannot be undone); then run setup.ps1 again." -ExitCode 2
            }
            Write-Host "  OK: no existing Collector install, token, secret, cursor or rules on this computer." -ForegroundColor Green
            $detectedMode = "fresh"
            Write-SetupEvent -Type "mode_detected" -Fields @{ mode = $detectedMode; contract = $null }
            Complete-SetupStage
        }

        # === STEP 3: source files ======================================================
        Write-Host ""
        Write-Host "=== Step 3: Tech Logic source files ===" -ForegroundColor Cyan
        $sources = $null
        if ($mode -eq "ResumeFinish") {
            Write-Host "  Skipped: the installed config already records them (the verification steps below check the files)." -ForegroundColor Green
            Skip-SetupStage -Id "sources" -Reason "already_installed"
        } else {
            Enter-SetupStage -Id "sources"
            $sources = Get-SourceSelection -Defaults $defaults -Checkins $CheckinsPath -Rejects $RejectsPath -Acs $AcsPath
            Write-Host "  OK: all three source files exist." -ForegroundColor Green
            Complete-SetupStage
        }

        # === STEP 4: the API is reachable over HTTPS ====================================
        Write-Host ""
        Write-Host "=== Step 4: connecting to SortView ===" -ForegroundColor Cyan
        Enter-SetupStage -Id "connection"
        $reach = Test-ApiReachable -ApiUrl $resolvedApiUrl
        if (-not $reach.Ok) {
            Stop-Setup -Step "step 4 (connection)" -Problem "Could not reach $resolvedApiUrl over HTTPS ($($reach.Detail)). This is usually a network, proxy, firewall or certificate problem on this computer." `
                -Unchanged "nothing was installed$(if ($mode -eq 'Fresh') { ' and the enrollment code was NOT used' })" `
                -Fix "Check this computer's internet access to that address (and any proxy/TLS-inspection policy) with your IT staff, then run setup.ps1 again$(if ($mode -eq 'Fresh') { ' with the same code' })." -ExitCode 1
        }
        Write-Host "  OK: reached the SortView API." -ForegroundColor Green
        Complete-SetupStage

        if ($mode -eq "Fresh") {
            # The last check before the code: the folder a resume depends on can be created, restricted and
            # written. After the code is used, saving the enrollment there is the one step that must not fail.
            Enter-SetupStage -Id "setup_folder"
            try {
                Initialize-SetupStaging -DataRoot $DataRoot | Out-Null
                $script:State.StagingPrepared = $true
            } catch {
                Stop-Setup -Step "step 4 (setup folder)" -Problem "The setup folder '$(Join-Path $DataRoot 'setup')' could not be created, restricted to Administrators and SYSTEM, and written to ($($_.Exception.Message))." `
                    -Unchanged "nothing was installed and the enrollment code was NOT requested" `
                    -Fix "Make sure '$DataRoot' can be created and written by Administrators on this computer, then run setup.ps1 again." -ExitCode 1
            }
            Write-Host "  OK: the setup folder is ready (Administrators and SYSTEM only)." -ForegroundColor Green
            Complete-SetupStage

            # === STEP 5: redeem the enrollment code ====================================
            Write-Host ""
            Write-Host "=== Step 5: enrolling this computer ===" -ForegroundColor Cyan
            Enter-SetupStage -Id "enrollment"
            $code = $EnrollmentCode
            if ($null -eq $code) { $code = Read-EnrollmentCodeSecure }
            if ($null -eq $code -or $code.Length -eq 0) {
                Stop-Setup -Step "step 5 (enrollment)" -Problem "No enrollment code was entered." `
                    -Unchanged "nothing was installed" -Fix "Run setup.ps1 from an interactive PowerShell window and enter the code when asked." -ExitCode 1
            }
            $script:State.CodeAttempted = $true
            # This release enrolls on Contract v2 and asks for it first. A code issued for the legacy contract
            # answers `contract_not_permitted` WITHOUT being used; then -- and only then, and only once -- the
            # legacy request is sent with the same code. No other refusal, error or status is retried.
            $enrollment = Invoke-Enrollment -ApiUrl $resolvedApiUrl -Code $code -HostName (Get-LocalHostName) -Version $runtimeVersion -ContractVersion 2
            Write-SetupEnrollmentResult -RequestedContract 2 -Outcome $enrollment.Outcome -RefusalCode $enrollment.RefusalCode -Contract $enrollment.Contract
            if ($enrollment.Outcome -eq "Refused" -and $enrollment.RefusalCode -eq "contract_not_permitted") {
                Write-Host "  This enrollment code is for the legacy contract (the first request did not use it); enrolling on that contract."
                $enrollment = Invoke-Enrollment -ApiUrl $resolvedApiUrl -Code $code -HostName (Get-LocalHostName) -Version $runtimeVersion -ContractVersion 1
                Write-SetupEnrollmentResult -RequestedContract 1 -Outcome $enrollment.Outcome -RefusalCode $enrollment.RefusalCode -Contract $enrollment.Contract
            }
            $code = $null
            if ($enrollment.Outcome -eq "Refused") {
                # One of the API's fixed refusals: it GUARANTEES the code was not used.
                $script:State.CodeAttempted = $false
                $fix = switch ($enrollment.RefusalCode) {
                    "collector_upgrade_required" { "Get the current SortView Collector release bundle from SortView and run its setup.ps1 with the SAME code (it is still valid until it expires)." }
                    "enrollment_not_ready" { "If an earlier setup attempt for this installation ended with an unknown result (no response, or an error from SortView), or this branch already has a Collector, contact SortView support and do NOT ask for another enrollment code: the enrollment may already exist. Otherwise ask your SortView administrator to finish preparing this installation in Super Admin (the site's Collector time zone and its reviewed classification lists). The same code is still valid until it expires." }
                    default { "Contact SortView support; this release bundle and the SortView server do not agree on a contract. The same code is still valid until it expires." }
                }
                Stop-Setup -Step "step 5 (enrollment)" -Problem $enrollment.Detail `
                    -Unchanged "nothing was installed and no token was stored" -Fix $fix -ExitCode 1
            }
            if ($enrollment.Outcome -ne "Enrolled") {
                # What each outcome says about the single-use code:
                #   RateLimited, Redirected -- the API never processed the request: the code was NOT used.
                #   Rejected (400)          -- THIS request enrolled nothing. It cannot tell a mistyped or expired code
                #                              from one an EARLIER request already used, so it claims neither.
                #   everything else         -- UNKNOWN. No response at all (a timeout, a reset, a connection closed
                #                              after the request was sent), a 5xx, an unexpected status, or a 200
                #                              that could not be read: the server may have enrolled this computer and
                #                              the answer was lost. A Contract v2 enrollment exists exactly once and a
                #                              second code would be refused, so a NEW code is never the advice here.
                $definitelyNotUsed = @("RateLimited", "Redirected") -contains $enrollment.Outcome
                $sameCode = "Do NOT ask for a new enrollment code. Run setup.ps1 again with the SAME code. If the same code is then refused as invalid or already used, the first request may have completed: contact SortView support and do not request another code."
                $fix = switch ($enrollment.Outcome) {
                    "Rejected" { "If an earlier setup attempt with this code on this computer ended with an unknown result (no response, or an error from SortView), that attempt may have completed: contact SortView support and do NOT ask for a new enrollment code. Otherwise -- the code was mistyped, has expired, or was already used elsewhere -- check it and try again, or ask your SortView administrator for a new enrollment code." }
                    "RateLimited" { "Wait a minute, then run setup.ps1 again with the same code." }
                    "Unreachable" { "Check this computer's network/proxy. $sameCode" }
                    "Redirected" { "Contact SortView support; the API address in this bundle may be wrong. Do not use a different address unless SortView tells you to." }
                    default { $sameCode }
                }
                if ($definitelyNotUsed) { $script:State.CodeAttempted = $false }
                if ($enrollment.Outcome -eq "Rejected") {
                    $script:State.CodeAttempted = $false
                    $script:State.CodeRejected = $true
                }
                Stop-Setup -Step "step 5 (enrollment)" -Problem $enrollment.Detail `
                    -Unchanged "nothing was installed and no token was stored" -Fix $fix -ExitCode 1
            }
            $script:State.CodeConsumed = $true
            $script:State.Contract = $enrollment.Contract
            $customerId = $enrollment.CustomerId
            $branchId = $enrollment.BranchId
            $installationId = $enrollment.InstallationId
            Write-Host "  OK: this computer is enrolled (installation $installationId$(if ($enrollment.Contract -eq 2) { ', Contract v2' })). " -ForegroundColor Green
            Complete-SetupStage
          if ($enrollment.Contract -eq 2) {
            # === STEP 6 (Contract v2): save what a resume needs FIRST, then store the token ============
            # A Contract v2 enrollment has already created this branch's one v2 identity on the server, and
            # a second code would be refused. So everything non-secret the server just said -- the IDs, the
            # key id, the time zone, and the classification rules -- is put on disk, read back and checked
            # BEFORE anything else is attempted. From then on a local failure is resumed, never re-enrolled.
            Write-Host ""
            Write-Host "=== Step 6: saving the enrollment and the classification rules ===" -ForegroundColor Cyan
            Enter-SetupStage -Id "enrollment_save"
            $keyId = $enrollment.KeyId
            $timezone = $enrollment.Timezone
            $recoveryFailure = $null
            try {
                $rulesSha256 = Save-StagedRules -DataRoot $DataRoot -Rules $enrollment.Rules
                $recoveryJson = ConvertTo-SetupRecoveryJson -CustomerId $customerId -BranchId $branchId -InstallationId $installationId `
                    -ApiUrl $resolvedApiUrl -ReleaseVersion $runtimeVersion -ContractVersion 2 -KeyId $keyId -Timezone $timezone -RulesSha256 $rulesSha256
                $savedPath = Save-SetupRecovery -DataRoot $DataRoot -Json $recoveryJson
                $script:State.RecoveryPath = $savedPath
                $v2Problems = @(Confirm-SetupRecovery -DataRoot $DataRoot -CustomerId $customerId -BranchId $branchId `
                        -InstallationId $installationId -ApiUrl $resolvedApiUrl -ReleaseVersion $runtimeVersion `
                        -ContractVersion 2 -KeyId $keyId -Timezone $timezone -RulesSha256 $rulesSha256)
                if ($v2Problems.Count -gt 0) { $recoveryFailure = "could not be verified ($($v2Problems -join '; '))" }
            } catch {
                $recoveryFailure = "could not be saved ($($_.Exception.Message))"
            } finally {
                $enrollment.Rules = $null
            }
            if ($null -ne $recoveryFailure) {
                # What was written is unverified, so it must not steer a later resume.
                try {
                    Remove-SetupRecovery -DataRoot $DataRoot
                } catch {
                    Write-Host "  NOTE: the unverified files in '$(Join-Path $DataRoot 'setup')' could not be removed ($($_.Exception.Message))." -ForegroundColor Yellow
                }
                Stop-Setup -Step "step 6 (saving the enrollment)" -Problem "The enrollment details and classification rules $recoveryFailure, so setup will not go on." `
                    -Unchanged "nothing was installed, no token was stored and no Scheduled Task was created or changed" `
                    -Fix "Contact SortView support and give them installation number $installationId. Do NOT ask for another enrollment code: it would be refused." -ExitCode 1
            }
            $script:State.RecoveryOnDisk = $true
            Write-Host "  OK: the enrollment details and the classification rules are saved and verified (Administrators/SYSTEM only; never shown)." -ForegroundColor Green
            Complete-SetupStage

            Write-Host ""
            Write-Host "=== Step 6b: storing the API token ===" -ForegroundColor Cyan
            Enter-SetupStage -Id "token_store"
            $toolExit = Invoke-SetupTool -Path $setTokenScript -Parameters @{
                Token = $enrollment.Token; ExePath = $runtimeExe; CustomerId = $customerId; BranchId = $branchId; DataRoot = $DataRoot
            }
            $enrollment.Token = $null
            $tokenProblem = $null
            if ($toolExit -ne 0) {
                $tokenProblem = "The API token could not be stored (exit code $toolExit)."
            } else {
                $checkExit = Invoke-ApiTokenCheck -ExePath $runtimeExe -CustomerId $customerId -BranchId $branchId -DataRoot $DataRoot
                if ($checkExit -ne 0) { $tokenProblem = "After storing it, the Collector could not verify the stored API token (api-token check exit code $checkExit)." }
            }
            if ($null -ne $tokenProblem) {
                Stop-Setup -Step "step 6b (API token)" -Problem $tokenProblem `
                    -Unchanged "nothing was installed; the saved enrollment details and classification rules stay in place" `
                    -Fix "Contact SortView support: a replacement API token must be issued for installation $installationId. Store it with tools\set-api-token.ps1 -CustomerId $customerId -BranchId $branchId -DataRoot `"$DataRoot`" -ExePath `"$runtimeExe`", then run setup.ps1 again -- it resumes. Do NOT ask for another enrollment code." -ExitCode 1
            }
            $script:State.TokenStored = $true
            Write-Host "  OK: token stored in api_token.dpapi (DPAPI, Administrators + SYSTEM only; value never shown)." -ForegroundColor Green
            Complete-SetupStage
          } else {
            # === STEP 6: store the token, immediately; then save what a resume needs ====
            Write-Host ""
            Write-Host "=== Step 6: storing the API token ===" -ForegroundColor Cyan
            Enter-SetupStage -Id "token_store"
            # Stored with the BUNDLE's runtime (nothing is installed yet), for exactly the customer/branch and
            # data root install.ps1 will then write -- so the file is where the installed config will look.
            $toolExit = Invoke-SetupTool -Path $setTokenScript -Parameters @{
                Token = $enrollment.Token; ExePath = $runtimeExe; CustomerId = $customerId; BranchId = $branchId; DataRoot = $DataRoot
            }
            $enrollment.Token = $null
            if ($toolExit -ne 0) {
                Stop-Setup -Step "step 6 (API token)" -Problem "The token could not be stored (exit code $toolExit)." `
                    -Unchanged "nothing was installed" -Fix "Ask your SortView administrator for a new enrollment code, then run setup.ps1 again." -ExitCode 1
            }
            $checkExit = Invoke-ApiTokenCheck -ExePath $runtimeExe -CustomerId $customerId -BranchId $branchId -DataRoot $DataRoot
            if ($checkExit -ne 0) {
                Stop-Setup -Step "step 6 (API token)" -Problem "After storing it, the Collector could not verify the stored API token (api-token check exit code $checkExit)." `
                    -Unchanged "nothing was installed" -Fix "Ask your SortView administrator for a new enrollment code, then run setup.ps1 again." -ExitCode 1
            }
            $script:State.TokenStored = $true
            Write-Host "  OK: token stored in api_token.dpapi (DPAPI, Administrators + SYSTEM only; value never shown)." -ForegroundColor Green
            Complete-SetupStage
            Enter-SetupStage -Id "enrollment_save"

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
                    -Fix "Fix what is named above (the folder '$(Join-Path $DataRoot 'setup')' must be writable by Administrators) and ask your SortView administrator for a NEW enrollment code. The token just stored belongs to the enrollment that could not be saved: setup will not replace it, so remove '$DataRoot\secrets\api_token.dpapi' yourself before running setup.ps1 again." -ExitCode 1
            }
            $script:State.RecoveryOnDisk = $true
            Write-Host "  OK: the non-secret enrollment details were saved and verified (Administrators/SYSTEM only), so an interrupted setup can resume without a new code." -ForegroundColor Green
            Complete-SetupStage
          }
        } else {
            Write-Host ""
            Write-Host "=== Steps 5-6: not needed -- resuming with the saved enrollment and the token already stored on this computer ===" -ForegroundColor Cyan
            # None of these ran in this invocation: they were done by the run that saved the enrollment.
            $resumedStages = @("setup_folder", "enrollment", "token_store", "enrollment_save")
            if ($script:State.Contract -eq 2) { $resumedStages = @("setup_folder", "enrollment", "enrollment_save", "token_store") }
            foreach ($resumedStage in $resumedStages) { Skip-SetupStage -Id $resumedStage -Reason "resume" }
        }

        # === STEP 7: install ===========================================================
        Write-Host ""
        Write-Host "=== Step 7: installing the Collector ===" -ForegroundColor Cyan
        $script:State.InstallStarted = $true
        if ($mode -eq "ResumeFinish") {
            Write-Host "  Skipped: the matching install is already in place." -ForegroundColor Green
            Skip-SetupStage -Id "install" -Reason "already_installed"
        } else {
            Enter-SetupStage -Id "install"
            $installParameters = @{
                InstallRoot = $InstallRoot; DataRoot = $DataRoot
                CustomerId = $customerId; BranchId = $branchId; InstallationId = $installationId
                ApiUrl = $resolvedApiUrl
                CheckinsPath = $sources["checkins"]; RejectsPath = $sources["rejects"]; AcsPath = $sources["acs"]
                SuppressNextSteps = $true
            }
            if ($script:State.Contract -eq 2) {
                # The config selects Contract v2 with the key id and time zone the server issued (non-secret).
                # Everything else about v2 is left to the Collector's own defaults.
                $installParameters["ContractMode"] = "v2"
                $installParameters["KeyId"] = $(if ($mode -eq "Fresh") { $keyId } else { $record.KeyId })
                $installParameters["Timezone"] = $(if ($mode -eq "Fresh") { $timezone } else { $record.Timezone })
            }
            $toolExit = Invoke-SetupTool -Path $installScript -Parameters $installParameters
            if ($toolExit -ne 0) {
                Stop-Setup -Step "step 7 (install)" -Problem "install.ps1 did not finish (exit code $toolExit) -- see its message above." `
                    -Unchanged "the Scheduled Task was not created" `
                    -Fix "Fix what the message above names, then run setup.ps1 again: it resumes with the saved enrollment (no new enrollment code is needed). If install.ps1 left a partial install behind, run tools\uninstall.ps1 first -- the saved enrollment and the stored token are kept." -ExitCode $toolExit
            }
            Complete-SetupStage
        }

        # === STEP 7b (Contract v2): the site's classification rules ======================
        if ($script:State.Contract -eq 2) {
            Write-Host ""
            Write-Host "=== Step 7b: installing the classification rules ===" -ForegroundColor Cyan
            Enter-SetupStage -Id "rules_install"
            $expectedRulesSha256 = $(if ($mode -eq "Fresh") { $rulesSha256 } else { $record.RulesSha256 })
            $rulesOutcome = $null
            try {
                $rulesOutcome = Install-ClassificationRules -DataRoot $DataRoot -ExpectedSha256 $expectedRulesSha256
            } catch {
                Stop-Setup -Step "step 7b (classification rules)" -Problem "The classification rules SortView issued for this installation could not be installed: $($_.Exception.Message)." `
                    -Unchanged "every rules file; the Scheduled Task was not created" `
                    -Fix "Run setup.ps1 again -- it resumes with the saved enrollment. If this repeats, contact SortView support. Do NOT ask for another enrollment code." -ExitCode 2
            }
            Write-Host "  OK: the classification rules from SortView are $(if ($rulesOutcome -eq 'Installed') { 'installed' } else { 'already installed' }) (Administrators/SYSTEM only; never shown)." -ForegroundColor Green
            Complete-SetupStage -Outcome $(if ($rulesOutcome -eq "Installed") { "ok" } else { "already_done" })
        } else {
            Skip-SetupStage -Id "rules_install" -Reason "not_applicable"
        }

        # === STEP 8: preflights, bootstrap, task registration (DISABLED) ================
        Write-Host ""
        Write-Host "=== Step 8: verifying and registering (preflight, bootstrap, Scheduled Task) ===" -ForegroundColor Cyan
        Enter-SetupStage -Id "verification"
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
        Complete-SetupStage

        # Setup has completed: the saved enrollment has done its job.
        Enter-SetupStage -Id "record_cleanup"
        $cleanupOutcome = "ok"
        try {
            Remove-SetupRecovery -DataRoot $DataRoot
            $script:State.RecoveryOnDisk = $false
        } catch {
            Write-Host "  Note: the saved (non-secret) enrollment record could not be removed: $($_.Exception.Message)" -ForegroundColor Yellow
            $cleanupOutcome = "warning"
        }
        Complete-SetupStage -Outcome $cleanupOutcome

        # === STEP 9: optional, explicit enable =========================================
        Write-Host ""
        Write-Host "=== SortView Collector setup COMPLETE. The task is registered DISABLED. ===" -ForegroundColor Green
        Write-Host ""
        Write-Host "  [OK] Release bundle verified against its manifest"
        Write-Host "  [OK] Enrolled with SortView (installation $installationId)$(if ($mode -ne 'Fresh') { ' -- resumed from the saved enrollment' }); token stored (never shown)"
        Write-Host "  [OK] Installed from release $runtimeVersion"
        Write-Host "  [OK] Interactive and SYSTEM-context preflight passed"
        Write-Host "  [OK] Starting cursors seeded for every source"
        if ($script:State.Contract -eq 2) {
            Write-Host "  [OK] Contract v2: classification rules from SortView installed; local secret created on this machine (neither is ever shown)"
        }
        Write-Host "  [OK] Scheduled Task '$($script:TaskName)' registered -- State: Disabled"
        Write-Host ""

        # A Contract v2 install is enabled here, as the LAST action and only because every step above succeeded:
        # both preflights passed, the cursor is seeded at the end of each file, and the task was just verified
        # Disabled. A legacy install keeps its explicit question (Enter = leave it disabled).
        Enter-SetupStage -Id "task_enable"
        $isV2Install = ($script:State.Contract -eq 2)
        $enableNow = ([bool]$EnableTask -or $isV2Install)
        if ($isV2Install) {
            Write-Host "Enabling the scheduled task: this Contract v2 install passed every check, so it starts on its own schedule."
        }
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
            Complete-SetupStage -Outcome "enabled"
        } else {
            Write-Host "The Scheduled Task was left DISABLED. When you are ready:"
            Write-Host "    Enable-ScheduledTask -TaskName '$($script:TaskName)'"
            Write-Host "Optional immediate run after enabling (otherwise it runs at its next 15-minute trigger):"
            Write-Host "    Start-ScheduledTask -TaskName '$($script:TaskName)'"
            Write-Host "This script did not start a Collector run."
            Complete-SetupStage -Outcome "left_disabled"
        }
        $exitCode = 0
        $runResult = "completed"
    } catch {
        $stopCode = $null
        if ($_.Exception.Data.Contains("SortViewSetupExitCode")) { $stopCode = [int]$_.Exception.Data["SortViewSetupExitCode"] }
        if ($null -ne $stopCode) {
            $exitCode = $stopCode
            $runResult = "stopped"
        } else {
            # An unexpected error: the type and message only, never any variable's contents.
            Write-Host ""
            Write-Host "SETUP STOPPED: unexpected error: $($_.Exception.GetType().Name): $($_.Exception.Message)" -ForegroundColor Red
            foreach ($line in (Get-EnrollmentStateLines)) { Write-Host "  $line" }
            $exitCode = 1
            if ($script:SetupEventsEnabled) {
                # The same type name and message just printed -- never the exception object or anything it holds.
                $failure = $_
                try {
                    $fields = Get-SetupEventRunState
                    $fields["exception_type"] = [string]$failure.Exception.GetType().Name
                    $fields["message"] = [string]$failure.Exception.Message
                    $fields["advice"] = @(Get-EnrollmentStateLines)
                    Write-SetupEvent -Type "run_failed_unexpected" -Fields $fields
                } catch {
                    $null = $_
                }
                $failure = $null
            }
        }
    } finally {
        # Best-effort scrubbing of secret material held by this run.
        if ($null -ne $enrollment) {
            $enrollment.Token = $null
        }
        $code = $null
        $enrollment = $null
        # The setup folder this run prepared for an enrollment that then certainly did not happen holds
        # nothing: it is taken away again (only if empty -- Remove-SetupRecovery never removes anything else).
        if ($script:State.StagingPrepared -and -not $script:State.CodeConsumed -and -not $script:State.CodeAttempted) {
            try {
                Remove-SetupRecovery -DataRoot $DataRoot
            } catch {
                $null = $_
            }
        }
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
    if ($script:SetupEventsEnabled) {
        # The one final event of every run, after the cleanup above: the exit code this function is about to return.
        try {
            $finalState = Get-SetupEventRunState
            Write-SetupEvent -Type "run_finished" -Fields @{
                result = $runResult; exit_code = [int]$exitCode; mode = $detectedMode; contract = $finalState["contract"]
                task_state = $(if ($runResult -ne "completed") { $null } elseif ($script:State.TaskEnabledOnPurpose) { "Enabled" } else { "Disabled" })
                release_version = $runtimeVersion; installation_id = $installationId
            }
        } catch {
            $null = $_
        }
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
