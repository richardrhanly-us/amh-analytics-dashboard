$rec = @{ tool = 'install'; params = @{} }
foreach ($k in $PSBoundParameters.Keys) { $v = $PSBoundParameters[$k]; if ($v -is [switch]) { $v = [bool]$v }; if ($v -is [securestring]) { $v = '<securestring>' }; $rec.params[$k] = $v }

if ($VerifyBundleOnly) {
    $rec['tool'] = 'install-verify'
Add-Content -LiteralPath "$env:SETUPTEST_FAKE_DIR\calls.jsonl" -Value ($rec | ConvertTo-Json -Compress -Depth 6)

    if ($env:SETUPTEST_VERIFY_MODE -eq 'fail') { Write-Host 'STUB install.ps1: MANIFEST VERIFICATION FAILED'; exit 4 }
    Write-Host 'STUB install.ps1: bundle verified (read-only)'
    exit 0
}
Add-Content -LiteralPath "$env:SETUPTEST_FAKE_DIR\calls.jsonl" -Value ($rec | ConvertTo-Json -Compress -Depth 6)

if ($env:SETUPTEST_INSTALL_MODE -eq 'fail') { Write-Host 'STUB install.ps1: FAILED'; exit 3 }
New-Item -ItemType Directory -Force -Path $InstallRoot, (Join-Path $DataRoot 'config'), (Join-Path $DataRoot 'data') | Out-Null
Set-Content -LiteralPath (Join-Path $InstallRoot 'SortViewCollector.exe') -Value 'fake'
$config = [ordered]@{
    customer_id = [int]$CustomerId; branch_id = [int]$BranchId; installation_id = [int]$InstallationId; api_url = $ApiUrl
    sources = @(
        [ordered]@{ name = 'checkins'; path = $CheckinsPath }, [ordered]@{ name = 'rejects'; path = $RejectsPath },
        [ordered]@{ name = 'acs'; path = $AcsPath })
    state_path = (Join-Path $DataRoot 'data\state.json')
}
if ($ContractMode) { $config['contract_mode'] = $ContractMode; $config['v2'] = [ordered]@{ key_id = $KeyId; timezone = $Timezone } }
Set-Content -LiteralPath (Join-Path $DataRoot 'config\collector_config.json') -Value ($config | ConvertTo-Json -Depth 5)
Write-Host 'STUB install.ps1: installed'
exit 0
