
function Add-Call { param($Record) Add-Content -LiteralPath "$FakeDir\calls.jsonl" -Value ($Record | ConvertTo-Json -Compress -Depth 6) }
function Test-IsAdministrator { @@ADMIN@@ }
function Get-LocalHostName { 'TEST-PC' }
function Get-RuntimeVersion { param($ExePath) Add-Call @{ tool = 'runtime-version' }; '@@RUNTIME_VERSION@@' }
function Test-ApiTokenStored { param($DataRoot) Test-Path "$FakeDir\machine_token.txt" }
function Invoke-ApiTokenCheck {
    param($ExePath, $CustomerId, $BranchId, $DataRoot)
    if ($env:SETUPTEST_CHECK_MODE -eq 'fail') { return 1 }
    if (Test-Path "$FakeDir\machine_token.txt") { return 0 } else { return 3 }
}
function Get-SortViewTask { if (Test-Path "$FakeDir\task.json") { Get-Content -LiteralPath "$FakeDir\task.json" -Raw | ConvertFrom-Json } else { $null } }
function Set-FakeTaskState { param($State) $t = Get-SortViewTask; $t.State = $State; $t | ConvertTo-Json | Set-Content -LiteralPath "$FakeDir\task.json" }
function Enable-SortViewTask { Add-Call @{ tool = 'enable-task' }; if ($env:SETUPTEST_ENABLE_FAILS) { throw 'enable failed' }; Set-FakeTaskState 'Ready' }
function Disable-SortViewTask { Add-Call @{ tool = 'disable-task' }; Set-FakeTaskState 'Disabled' }
function Protect-SetupRecoveryDirectory { param($Path) Add-Call @{ tool = 'protect-recovery'; path = $Path }; if ($env:SETUPTEST_PROTECT_FAILS) { throw 'could not restrict the folder' } }
function Read-SetupAnswer {
    param([string]$Prompt)
    # One answer per line ("<ENTER>" = the empty answer); an empty/absent queue = a host that cannot be asked.
    $lines = @(); if (Test-Path "$FakeDir\answers.txt") { $lines = @(Get-Content -LiteralPath "$FakeDir\answers.txt") }
    Add-Call @{ tool = 'prompt'; text = $Prompt }
    if ($lines.Count -eq 0) { return $null }
    $first = [string]$lines[0]
    $rest = @($lines | Select-Object -Skip 1)
    if ($rest.Count -gt 0) { Set-Content -LiteralPath "$FakeDir\answers.txt" -Value $rest } else { Remove-Item -LiteralPath "$FakeDir\answers.txt" }
    if ($first -eq '<ENTER>') { return '' }
    return $first
}
function Read-EnrollmentCodeSecure { Add-Call @{ tool = 'read-code' }; if ($env:SETUPTEST_NO_CODE) { return $null }; ConvertTo-SecureString '@@CODE@@' -AsPlainText -Force }
function Invoke-SetupHttp {
    param($Method, $Url, $Body)
    $parsed = $null; if ($Body) { $parsed = $Body | ConvertFrom-Json }
    Add-Call @{ tool = 'http'; method = $Method; url = $Url; body = $parsed }
    $key = "$Method " + ([Uri]$Url).AbsolutePath
    $script = Get-Content -LiteralPath "$FakeDir\http.json" -Raw | ConvertFrom-Json
    $entry = $script.PSObject.Properties[$key]
    if ($null -eq $entry) { return [pscustomobject]@{ StatusCode = 0; Content = $null; Failure = "no scripted response for $key" } }
    $e = $entry.Value
    if ($e -is [array]) {
        # A SEQUENCE of answers for one route: the 1st call gets the 1st, ... and the last one repeats.
        $counter = Join-Path $FakeDir ("http_count_" + ($key -replace '[^A-Za-z0-9]', '_') + ".txt")
        $seen = 0; if (Test-Path -LiteralPath $counter) { $seen = [int](Get-Content -LiteralPath $counter -Raw) }
        Set-Content -LiteralPath $counter -Value ($seen + 1)
        $e = $e[[Math]::Min($seen, $e.Count - 1)]
    }
    return [pscustomobject]@{ StatusCode = [int]$e.status; Content = [string]$e.content; Failure = $e.failure }
}
