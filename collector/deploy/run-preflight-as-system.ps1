<#
.SYNOPSIS
    Runs collector/preflight.py under the SAME security principal the
    production Scheduled Task will actually use (SYSTEM by default) --
    NOT the interactive account running this script. This is the ONLY
    way to genuinely answer "can SYSTEM read the Tech Logic files, write
    state/status/logs, see the token, resolve DNS, and reach the API" --
    running preflight interactively proves none of that, since SYSTEM has
    its own file permissions and its own (largely empty) per-user proxy
    configuration.

.DESCRIPTION
    1. Registers a TEMPORARY, one-time Scheduled Task (a name distinct
       from -- and never confused with -- the real production task
       "SortView Collector", and from the legacy "sortview-scheduler"),
       running as -Principal, with no recurring trigger.
    2. Starts it immediately (Start-ScheduledTask) and polls until it
       finishes or -TimeoutSeconds elapses.
    3. Reads back the deterministic JSON result file
       collector/preflight.py wrote (--output) -- this is the reliable
       way to get a result out of a Scheduled Task; capturing its stdout
       directly is not a supported pattern on Windows.
    4. Prints a clear PASS/FAIL summary, explicitly labeled as a
       SYSTEM-context result, distinct from any earlier interactive run.
    5. ALWAYS unregisters the temporary task afterward (success, failure,
       or timeout alike) -- never leaves it behind. Does not touch the
       real production task or C:\SortViewAgent in any way.

    Does NOT modify Windows proxy settings, the trust store, or any
    system configuration -- if SYSTEM-context preflight fails on a
    network/TLS check, that is reported as-is; see
    docs/collector-v1-admin-guide.md's proxy/TLS troubleshooting section
    for what to do about it manually. This script never guesses or
    auto-configures a fix.

.EXAMPLE
    .\run-preflight-as-system.ps1 `
        -InstallRoot "C:\SortView\Collector" `
        -ConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json"
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\SortView\Collector",
    [string]$ConfigPath = "C:\ProgramData\SortViewCollector\config\collector_config.json",
    [string]$Principal = "SYSTEM",
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"

$TempTaskName = "SortView Collector Preflight (SYSTEM validation)"
if ($TempTaskName -like "*sortview-scheduler*" -or $TempTaskName -eq "SortView Collector") {
    throw "Refusing to use a temporary task name that could collide with a real task."
}

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an elevated (Administrator) PowerShell session."
}

$VenvPython = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$FrozenExe = Join-Path $InstallRoot "SortViewCollector.exe"
$IsFrozen = (Test-Path $FrozenExe) -and -not (Test-Path $VenvPython)
if (-not (Test-Path $VenvPython) -and -not (Test-Path $FrozenExe)) {
    throw "No installed runtime found at '$InstallRoot' (neither .venv\Scripts\python.exe nor " +
          "SortViewCollector.exe) -- run install.ps1 first."
}
$RunnerExe = if ($IsFrozen) { $FrozenExe } else { $VenvPython }

$ResultPath = Join-Path $env:TEMP "sortview-collector-preflight-system-$([guid]::NewGuid().ToString('N')).json"
# FROZEN: SortViewCollector.exe's own "preflight" dispatcher subcommand --
# no "-m collector.preflight" (there is no Python module system in a
# frozen process). SOURCE: unchanged. Everything else below (temporary
# task XML registration/start/wait/cleanup) is invocation-agnostic -- it
# just runs whatever $RunnerExe/$PreflightArgs resolve to, under the same
# real SYSTEM identity either way.
$PreflightArgs = if ($IsFrozen) {
    "preflight --config `"$ConfigPath`" --output `"$ResultPath`""
} else {
    "-m collector.preflight --config `"$ConfigPath`" --output `"$ResultPath`""
}

$cleanupDone = $false
function Remove-TempTask {
    if ($script:cleanupDone) { return }
    $existing = Get-ScheduledTask -TaskName $TempTaskName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.State -eq "Running") {
            Stop-ScheduledTask -TaskName $TempTaskName -ErrorAction SilentlyContinue
        }
        Unregister-ScheduledTask -TaskName $TempTaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    $script:cleanupDone = $true
}

$taskXmlPath = Join-Path $env:TEMP "sortview-collector-preflight-system-task-$([guid]::NewGuid().ToString('N')).xml"
try {
    Write-Host "Registering temporary task '$TempTaskName' (principal: $Principal)..."

    # Built as XML rather than `schtasks /create /tr "..."`, for two
    # reasons found by actually running this script against the
    # validation paths (not by inspection alone):
    #   1. `/tr` has an undocumented-until-you-hit-it 261-character limit
    #      on the whole command string -- the full venv python path plus
    #      --config/--output arguments (each an absolute path) exceeded
    #      it under Phase 4d's longer, deliberately-distinct validation
    #      paths (schtasks error: "Value for '/tr' option cannot be more
    #      than 261 character(s)"). XML has no such limit.
    #   2. A task created via plain `/tr` has no working directory of its
    #      own -- `python -m collector.preflight` then can't resolve the
    #      `collector` package (ModuleNotFoundError), since the package
    #      lives under -InstallRoot, not wherever Task Scheduler defaults
    #      an unset "start in" to. <WorkingDirectory> below fixes this the
    #      same way register-collector-task.ps1's real task XML already
    #      does.
    function Escape-TaskXml([string]$text) {
        return $text.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
    }
    $escapedCommand = Escape-TaskXml($RunnerExe)
    $escapedArgs = Escape-TaskXml($PreflightArgs)
    $escapedWorkingDir = Escape-TaskXml($InstallRoot)
    $escapedPrincipal = Escape-TaskXml($Principal)

    $taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Temporary SortView Collector SYSTEM-context preflight validation task. Self-removing. Never confused with the real production task or C:\SortViewAgent.</Description>
  </RegistrationInfo>
  <Triggers />
  <Principals>
    <Principal id="Author">
      <UserId>$escapedPrincipal</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$escapedCommand</Command>
      <Arguments>$escapedArgs</Arguments>
      <WorkingDirectory>$escapedWorkingDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
    [System.IO.File]::WriteAllText($taskXmlPath, $taskXml, [System.Text.Encoding]::Unicode)

    schtasks /create /xml $taskXmlPath /tn $TempTaskName /f | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "schtasks /create failed (exit $LASTEXITCODE)" }

    Write-Host "Starting it now (not waiting for the placeholder scheduled time)..."
    Start-ScheduledTask -TaskName $TempTaskName

    Write-Host "Waiting for it to finish (timeout: ${TimeoutSeconds}s)..."
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 2
        $info = Get-ScheduledTaskInfo -TaskName $TempTaskName
        $task = Get-ScheduledTask -TaskName $TempTaskName
    } while ($task.State -eq "Running" -and (Get-Date) -lt $deadline)

    if ($task.State -eq "Running") {
        Write-Host "TIMED OUT waiting for the SYSTEM-context preflight task to finish." -ForegroundColor Red
        Write-Host "SYSTEM CONTEXT VALIDATION: FAIL (timeout)" -ForegroundColor Red
        exit 1
    }

    if (-not (Test-Path $ResultPath)) {
        Write-Host "The task finished (LastTaskResult=$($info.LastTaskResult)) but never wrote a result file." -ForegroundColor Red
        Write-Host "This means preflight.py itself likely failed to even start under $Principal -- check" -ForegroundColor Red
        Write-Host "the venv path, the config path, and that $Principal has execute access to the venv." -ForegroundColor Red
        Write-Host "SYSTEM CONTEXT VALIDATION: FAIL (no result produced)" -ForegroundColor Red
        exit 1
    }

    $result = Get-Content $ResultPath -Raw | ConvertFrom-Json
    Write-Host ""
    Write-Host "=== SYSTEM-context preflight result (as $Principal) ===" -ForegroundColor Cyan
    foreach ($check in $result.checks) {
        $status = if ($check.passed) { "PASS" } else { "FAIL" }
        $color = if ($check.passed) { "Green" } else { "Red" }
        Write-Host "[$status] $($check.name): $($check.detail)" -ForegroundColor $color
    }
    Write-Host ""

    if ($result.passed) {
        Write-Host "SYSTEM CONTEXT VALIDATION: PASS" -ForegroundColor Green
        exit 0
    } else {
        Write-Host "SYSTEM CONTEXT VALIDATION: FAIL -- see failing check(s) above." -ForegroundColor Red
        Write-Host "This is a REAL result under the actual production run identity ($Principal) -- do not" -ForegroundColor Red
        Write-Host "register/start the production task until every check passes here, not just interactively." -ForegroundColor Red
        exit 1
    }
} finally {
    Remove-TempTask
    Remove-Item $ResultPath -ErrorAction SilentlyContinue
    Remove-Item $taskXmlPath -ErrorAction SilentlyContinue
    Write-Host "Temporary validation task and result file cleaned up."
}
