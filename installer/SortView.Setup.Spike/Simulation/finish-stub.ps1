$rec = @{ tool = 'finish-install'; params = @{} }
foreach ($k in $PSBoundParameters.Keys) { $v = $PSBoundParameters[$k]; if ($v -is [switch]) { $v = [bool]$v }; if ($v -is [securestring]) { $v = '<securestring>' }; $rec.params[$k] = $v }
Add-Content -LiteralPath "$env:SETUPTEST_FAKE_DIR\calls.jsonl" -Value ($rec | ConvertTo-Json -Compress -Depth 6)

$mode = $env:SETUPTEST_FINISH_MODE
function Register-FakeTask { param($State) @{ State = $State } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $env:SETUPTEST_FAKE_DIR 'task.json') }
switch ($mode) {
    'fail_interactive_preflight' { Write-Host '[FAIL] source_paths_exist: STUB'; Write-Host 'SETUP STOPPED at step 2 of 5 (interactive preflight)'; exit 1 }
    'fail_system_preflight'      { Write-Host 'SYSTEM-context preflight FAILED (STUB)'; Write-Host 'SETUP STOPPED at step 3 of 5 (SYSTEM-context preflight)'; exit 1 }
    'fail_bootstrap'             { Write-Host 'SETUP STOPPED at step 4 of 5 (bootstrap)'; exit 1 }
    'incomplete_state'           { Write-Host 'source acs has no entry -- the file does not cover every configured source.'; Write-Host 'SETUP STOPPED at step 4 of 5 (bootstrap)'; exit 2 }
    'fail_task_registration'     { Write-Host 'SETUP STOPPED at step 5 of 5 (Scheduled Task)'; exit 1 }
    'rogue_enabled_task_then_fail' { Register-FakeTask 'Ready'; Write-Host 'SETUP STOPPED at step 5 of 5 (Scheduled Task)'; exit 1 }
    'ok_but_task_enabled'        { Register-FakeTask 'Ready'; Write-Host 'finished (STUB)'; exit 0 }
    'ok_but_no_task'             { Write-Host 'finished (STUB)'; exit 0 }
    default                      { Register-FakeTask 'Disabled'; Write-Host 'STUB finish-install.ps1: setup complete, task registered Disabled'; exit 0 }
}
