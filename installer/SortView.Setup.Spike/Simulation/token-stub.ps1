$rec = @{ tool = 'set-api-token'; params = @{} }
foreach ($k in $PSBoundParameters.Keys) { $v = $PSBoundParameters[$k]; if ($v -is [switch]) { $v = [bool]$v }; if ($v -is [securestring]) { $v = '<securestring>' }; $rec.params[$k] = $v }

$rec['token_supplied'] = $PSBoundParameters.ContainsKey('Token')
$rec['token_type'] = if ($null -ne $Token) { $Token.GetType().Name } else { $null }
Add-Content -LiteralPath "$env:SETUPTEST_FAKE_DIR\calls.jsonl" -Value ($rec | ConvertTo-Json -Compress -Depth 6)

if ($env:SETUPTEST_TOKEN_MODE -eq 'fail') { throw 'STUB set-api-token: could not set the variable' }
if ($null -ne $Token) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Token)
    try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($env:SETUPTEST_TOKEN_MODE -eq 'wrong') { $plain = 'a-different-token-value-entirely-xyz' }
    Set-Content -LiteralPath (Join-Path $env:SETUPTEST_FAKE_DIR 'machine_token.txt') -Value $plain -NoNewline
}
Write-Host "STUB set-api-token: stored in api_token.dpapi (value not shown)."
