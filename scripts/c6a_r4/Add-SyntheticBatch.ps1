<#
.SYNOPSIS
    C6a R4: append one synthetic batch to this computer's Tech Logic files. Qualification VMs only.

.DESCRIPTION
    Appends the three parts of a batch made on the host by make_synthetic_batch.py to the Tech Logic files under
    -SourceFolder. It only ever APPENDS: it never creates, truncates, rewrites or reorders a file, and it never touches
    SortView Setup, the Collector, its configuration, its state or its cursor.

    Everything is checked before anything is written:
      - each part matches the SHA-256 in batch.json, is not empty and ends with a line break;
      - every ACS line has its two 0x02 separators;
      - each target file exists (Setup requires them) and is empty or ends with a line break, so a record is never
        joined onto the end of another;
      - this batch was not appended on this computer before (journal: -JournalPath).
    After writing, each file must be exactly its old bytes followed by the part.

    On a REPLACEMENT computer, append only after its first automatic Collector run: that run starts each file at its
    end, so anything appended earlier is never read.

    The data is synthetic (CANARY values) and never customer data. Windows PowerShell 5.1.

.PARAMETER BatchFolder
    The batch folder copied from the host (it holds batch.json and the three .part files).

.PARAMETER SourceFolder
    The Tech Logic folder this computer's Collector reads. Default: C:\TLCFinalDlls.

.PARAMETER JournalPath
    The local record of batches appended on this computer.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Add-SyntheticBatch.ps1 -BatchFolder C:\SortView-R4\batches\A1
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BatchFolder,
    [string]$SourceFolder = "C:\TLCFinalDlls",
    [string]$JournalPath = "C:\SortView-R4\appended-batches.log"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return (($hasher.ComputeHash($Bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
    } finally {
        $hasher.Dispose()
    }
}

function Read-SharedBytes {
    # The Collector may have the file open; read without asking for exclusive access.
    param([string]$Path)
    $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $buffer = New-Object byte[] $stream.Length
        $read = 0
        while ($read -lt $buffer.Length) {
            $count = $stream.Read($buffer, $read, $buffer.Length - $read)
            if ($count -eq 0) { throw "'$Path' became shorter while it was being read." }
            $read += $count
        }
        return , $buffer
    } finally {
        $stream.Dispose()
    }
}

function Get-ByteSlice {
    param([byte[]]$Bytes, [int]$Start, [int]$Count)
    $slice = New-Object byte[] $Count
    [Array]::Copy($Bytes, $Start, $slice, 0, $Count)
    return , $slice
}

$manifestPath = Join-Path $BatchFolder "batch.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "No batch.json in '$BatchFolder'." }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$label = [string]$manifest.label
if ($label -notmatch '^[A-Za-z0-9-]{1,40}$') { throw "batch.json has no usable label." }

if ((Test-Path -LiteralPath $JournalPath -PathType Leaf) -and
    (Select-String -LiteralPath $JournalPath -SimpleMatch " label=$label " -Quiet)) {
    throw "Batch '$label' was already appended on this computer (see '$JournalPath'). Make a new batch on the host instead."
}

# --- check everything before writing anything -------------------------------------------------------------------------
$plan = @()
foreach ($name in @("acs", "checkins", "rejects")) {
    $entry = $manifest.files.$name
    if ($null -eq $entry) { throw "batch.json has no '$name' part." }

    $partPath = Join-Path $BatchFolder ([string]$entry.part)
    $partBytes = [IO.File]::ReadAllBytes($partPath)
    if ((Get-Sha256Hex $partBytes) -ne [string]$entry.sha256) { throw "'$partPath' does not match its SHA-256 in batch.json." }
    if ($partBytes.Length -eq 0 -or $partBytes[$partBytes.Length - 1] -ne 10) { throw "'$partPath' is empty or does not end with a line break." }
    if ($name -eq "acs") {
        $lines = [Text.Encoding]::UTF8.GetString($partBytes).Split([char]10) | Where-Object { $_.Length -gt 0 }
        foreach ($line in $lines) {
            if (@($line.ToCharArray() | Where-Object { $_ -eq [char]2 }).Count -ne 2) {
                throw "An ACS line in '$partPath' does not have exactly two 0x02 separators; the batch is unusable."
            }
        }
    }

    $target = Join-Path $SourceFolder ([string]$entry.target)
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "'$target' does not exist. This script never creates a Tech Logic file." }
    $before = Read-SharedBytes $target
    if ($before.Length -gt 0 -and $before[$before.Length - 1] -ne 10) {
        throw "'$target' does not end with a line break; appending would join two records. Investigate before going on."
    }
    $plan += [pscustomobject]@{
        Name = $name; Target = $target; Part = $partBytes
        BeforeLength = $before.Length; BeforeSha256 = (Get-Sha256Hex $before)
    }
}

# --- append -----------------------------------------------------------------------------------------------------------
$summary = @()
foreach ($item in $plan) {
    $stream = New-Object IO.FileStream($item.Target, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
    try {
        $stream.Write($item.Part, 0, $item.Part.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }

    $after = Read-SharedBytes $item.Target
    if ($after.Length -ne $item.BeforeLength + $item.Part.Length) {
        throw "'$($item.Target)' is $($after.Length) bytes, expected $($item.BeforeLength + $item.Part.Length). Something else wrote to it: stop and investigate."
    }
    if ((Get-Sha256Hex (Get-ByteSlice $after 0 $item.BeforeLength)) -ne $item.BeforeSha256) {
        throw "The existing content of '$($item.Target)' changed. Stop and investigate."
    }
    if ((Get-Sha256Hex (Get-ByteSlice $after $item.BeforeLength $item.Part.Length)) -ne (Get-Sha256Hex $item.Part)) {
        throw "The appended bytes in '$($item.Target)' are not the batch's. Stop and investigate."
    }
    $summary += "$($item.Name)=+$($item.Part.Length)"
    Write-Host ("  {0,-9} +{1,6} bytes  {2} ({3} -> {4} bytes)" -f $item.Name, $item.Part.Length, $item.Target, $item.BeforeLength, $after.Length)
}

$journalFolder = Split-Path -Parent $JournalPath
if (-not (Test-Path -LiteralPath $journalFolder)) { New-Item -ItemType Directory -Path $journalFolder | Out-Null }
$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Add-Content -LiteralPath $JournalPath -Value "utc=$stamp label=$label $($summary -join ' ')" -Encoding UTF8

$expected = $manifest.expected_events
Write-Host ""
Write-Host "Batch '$label' appended at $stamp UTC. First record (local): $($manifest.first_record_local)."
Write-Host ("Expected after this computer's next successful run: ACS {0} hold, {1} non_hold_101, {2} other_code10; {3} check-ins; {4} rejects." -f `
        $expected.acs_item_events.hold, $expected.acs_item_events.non_hold_101, $expected.acs_item_events.other_code10,
    $expected.checkin_events, $expected.reject_events)
