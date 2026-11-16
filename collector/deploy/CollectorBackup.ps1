<#
.SYNOPSIS
    Restricted, verified runtime backups (tools\update.ps1) and the checks a
    restore makes before it trusts one (tools\restore-backup.ps1). 1.0.15.

.DESCRIPTION
    WHY THIS EXISTS (confirmed on NBPL's production sorter, 2026-11-16): an
    update's backup lives beside the install, as
    C:\SortView\Collector.backup-<timestamp>, and C:\SortView grants
    Authenticated Users Modify. Before 1.0.15 the backup was a plain
    Copy-Item into that parent, so it inherited that grant: any signed-in
    user could change a backup that the printed rollback would then copy,
    unverified, into the folder a SYSTEM task runs. (No tampering was
    observed; the finding is that it was possible.) See
    docs/PROJECT_MANIFEST.md, the canonical NBPL production sorter profile.

    WHAT THIS FILE GUARANTEES, and how:

      - A backup folder is CREATED restricted (New-ProtectedCollectorDirectory):
        the Administrators+SYSTEM-only DACL is part of the create call itself,
        never applied afterwards, so there is no moment at which the
        parent's broad grant reaches it. A path that already exists -- folder,
        file, junction or link, dangling ones included -- is refused, and so
        is a folder that was not created by this call (CreateDirectory hands
        back an existing folder without error; a different owner, any
        content, or a broader ACL proves it was not ours).
      - No path is ever used through a reparse point: not the path itself,
        not any of its ancestors (Assert-CollectorPathNotRedirected), and not
        anything inside a backup (Get-CollectorTreeFiles never follows one and
        reports it).
      - After the copy, every item is restricted (Protect-CollectorPath
        -Recurse), OWNED by Administrators (an owner keeps WRITE_DAC whatever
        the DACL says), read back, and the copy is compared file by file with
        its source -- Complete-ProtectedCollectorBackup throws on any problem,
        and its caller must then not remove or replace the install.
      - A restore trusts nothing the backup says about itself
        (Get-CollectorTrustedBackup): its MANIFEST.json must have the SHA-256
        the operator obtained OUTSIDE the backup (the release record, or a
        verified release artifact), and every release file is checked against
        THAT manifest. The backup's own MANIFEST.installed.json is never used:
        it sits in the very folder it would vouch for.

    Uses the well-known SIDs and the exact-policy verifier of
    CollectorAcl.ps1, and the path/manifest helpers of CollectorManifest.ps1;
    callers dot-source both first. Never touches the data root (config,
    secrets, classification rules, cursors) -- nothing here is given a path
    under it.
#>

Set-StrictMode -Version Latest

$Script:CollectorBackupMetadataFiles = @("MANIFEST.json", "MANIFEST.installed.json")

function Assert-CollectorPathNotRedirected {
    <#
    Throws if $Path, or any of its ancestors that exists, is a reparse point (a junction or a symbolic link). A
    path that does not exist yet is fine; the check covers what does.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $current = [System.IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrEmpty($current)) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "REDIRECTED PATH: '$current' is a junction or symbolic link -- refusing to use '$Path' through it."
        }
        $current = [System.IO.Path]::GetDirectoryName($current)
    }
}

function Test-CollectorEntryExists {
    <#
    Whether anything at all is at $Path -- a folder, a file, or a junction or link, INCLUDING a dangling one
    (Test-Path follows a link and reports a dangling one as absent; the parent's listing does not).
    #>
    param([Parameter(Mandatory)][string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    $parent = [System.IO.Path]::GetDirectoryName($full)
    $leaf = [System.IO.Path]::GetFileName($full)
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        return $false
    }
    return @(Get-ChildItem -LiteralPath $parent -Force -ErrorAction Stop | Where-Object { $_.Name -eq $leaf }).Count -gt 0
}

function Assert-CollectorBackupPathAvailable {
    <# Throws unless $Path is free (nothing there at all) and its parent is not reached through a reparse point. #>
    param([Parameter(Mandatory)][string]$Path)

    $parent = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($Path))
    Assert-CollectorPathNotRedirected -Path $parent
    if (Test-CollectorEntryExists -Path $Path) {
        throw "ALREADY EXISTS: something is already at '$Path' (a folder, file, junction or link). A backup or restore " +
              "only ever writes to a path it created itself. Inspect it, move it out of the way, then retry."
    }
}

function New-ProtectedCollectorDirectory {
    <#
    Creates $Path ALREADY restricted to SYSTEM and Administrators (inheritance cut; both grants flow to whatever is
    created inside it), then proves this call created it: not redirected, owned by this account or by
    Administrators/SYSTEM, empty, and exactly protected. Throws otherwise -- the caller must then write nothing.
    #>
    param([Parameter(Mandatory)][string]$Path)

    Assert-CollectorBackupPathAvailable -Path $Path

    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    foreach ($sid in $Script:CollectorAclAllowedSids) {
        $identity = New-Object System.Security.Principal.SecurityIdentifier($sid)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $identity, "FullControl", "ContainerInherit, ObjectInherit", "None", "Allow")
        $security.AddAccessRule($rule)
    }
    # The DACL is part of the create call (CreateDirectory with a security descriptor): never a broad moment.
    # .NET Framework (Windows PowerShell 5.1) has the overload on Directory; .NET (PowerShell 7) moved it.
    $overload = [System.IO.Directory].GetMethod("CreateDirectory", [type[]]@([string], [System.Security.AccessControl.DirectorySecurity]))
    if ($null -ne $overload) {
        [void][System.IO.Directory]::CreateDirectory($Path, $security)
    } else {
        [void][System.IO.FileSystemAclExtensions]::CreateDirectory($security, $Path)
    }

    # CreateDirectory returns an EXISTING folder without error: one that appeared after the check above is not ours.
    Assert-CollectorPathNotRedirected -Path $Path
    $owner = (Get-Acl -LiteralPath $Path).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    $acceptableOwners = @([System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value) + $Script:CollectorAclAllowedSids
    $isEmpty = @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop).Count -eq 0
    if (($owner -notin $acceptableOwners) -or -not $isEmpty -or -not (Test-CollectorPathProtected -Path $Path)) {
        throw "NOT CREATED BY THIS RUN: '$Path' is not an empty, restricted folder owned by this account -- " +
              "something else created it first. Nothing was written into it."
    }
}

function Get-CollectorTreeFiles {
    <#
    Every file under $Root, keyed by canonical relative path (CollectorManifest.ps1's comparer), walked folder by
    folder and NEVER through a reparse point: one anywhere, $Root included, is reported in Problems and not entered.
    #>
    param([Parameter(Mandatory)][string]$Root)

    $files = New-Object 'System.Collections.Generic.Dictionary[string,string]' ($Script:CollectorManifestComparer)
    $problems = @()
    $pending = New-Object 'System.Collections.Generic.Stack[object]'
    $pending.Push((Get-Item -LiteralPath $Root -Force))
    while ($pending.Count -gt 0) {
        $item = $pending.Pop()
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $problems += "REPARSE POINT (a junction or link, never followed): $($item.FullName)"
            continue
        }
        if ($item.PSIsContainer) {
            foreach ($child in @(Get-ChildItem -LiteralPath $item.FullName -Force -ErrorAction Stop)) { $pending.Push($child) }
        } else {
            $files[(Get-CollectorRelativePath -Root $Root -FullPath $item.FullName)] = $item.FullName
        }
    }
    return [pscustomobject]@{ Files = $files; Problems = $problems }
}

function Get-CollectorFileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Test-CollectorBackupCopy {
    <#
    The problems (none = a faithful copy): every expected file of $SourceRoot is in $BackupRoot with the same
    SHA-256, and $BackupRoot holds nothing else. Expected = -RelativePaths if given, otherwise every file under
    $SourceRoot. A reparse point on either side is a problem.
    #>
    param(
        [Parameter(Mandatory)][string]$SourceRoot,
        [Parameter(Mandatory)][string]$BackupRoot,
        [string[]]$RelativePaths
    )

    $source = Get-CollectorTreeFiles -Root $SourceRoot
    $backup = Get-CollectorTreeFiles -Root $BackupRoot
    $problems = @($source.Problems) + @($backup.Problems)
    if ($problems.Count -gt 0) {
        return $problems
    }

    $expected = if ($PSBoundParameters.ContainsKey("RelativePaths")) {
        @($RelativePaths | ForEach-Object { ConvertTo-CollectorCanonicalRelativePath $_ })
    } else {
        @($source.Files.Keys)
    }
    $expectedKeys = New-Object 'System.Collections.Generic.HashSet[string]' ($Script:CollectorManifestComparer)
    foreach ($relative in $expected) {
        [void]$expectedKeys.Add($relative)
        if (-not $source.Files.ContainsKey($relative)) {
            $problems += "SOURCE MISSING: $relative"
        } elseif (-not $backup.Files.ContainsKey($relative)) {
            $problems += "NOT COPIED: $relative"
        } elseif ((Get-CollectorFileSha256 $source.Files[$relative]) -ne (Get-CollectorFileSha256 $backup.Files[$relative])) {
            $problems += "COPY DIFFERS FROM THE INSTALL: $relative"
        }
    }
    foreach ($relative in @($backup.Files.Keys)) {
        if (-not $expectedKeys.Contains($relative)) {
            $problems += "UNEXPECTED FILE IN BACKUP: $relative"
        }
    }
    return $problems
}

function Test-CollectorRecordedFiles {
    <#
    The problems (none = intact): $Root holds MANIFEST.json and MANIFEST.installed.json, and every release file
    the latter records is present with its recorded SHA-256. Unlike Test-InstalledManifest, a file it does not
    record is not a problem here -- a frozen install root can legitimately hold files no release placed (the
    parser logs\ that releases before 1.0.14 wrote under it); Test-CollectorBackupCopy already requires the
    backup to be an exact copy of the install, those files included.
    #>
    param([Parameter(Mandatory)][string]$Root)

    $problems = @()
    foreach ($name in $Script:CollectorBackupMetadataFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $name) -PathType Leaf)) {
            $problems += "MANIFEST MISSING: $name (an install without release-manifest records cannot be backed up verifiably)"
        }
    }
    if ($problems.Count -gt 0) {
        return $problems
    }

    try {
        $recorded = Get-Content -LiteralPath (Join-Path $Root "MANIFEST.installed.json") -Raw | ConvertFrom-Json
    } catch {
        return @("MALFORMED MANIFEST: MANIFEST.installed.json is not valid JSON ($($_.Exception.Message))")
    }
    if (-not ($recorded.PSObject.Properties.Name -contains "files") -or @($recorded.files).Count -eq 0) {
        return @("MALFORMED MANIFEST: MANIFEST.installed.json records no files")
    }

    foreach ($entry in @($recorded.files)) {
        if (-not (Test-CollectorManifestPathSafe $entry.installed_path)) {
            $problems += "UNSAFE MANIFEST PATH: $($entry.installed_path)"
            continue
        }
        $path = Join-Path $Root (ConvertTo-CollectorCanonicalRelativePath $entry.installed_path)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $problems += "MISSING: $($entry.installed_path)"
        } elseif ((Get-CollectorFileSha256 $path) -ne $entry.sha256.ToUpperInvariant()) {
            $problems += "HASH MISMATCH: $($entry.installed_path)"
        }
    }
    return $problems
}

function Set-CollectorTreeOwner {
    <#
    Makes Administrators the owner of $Path and everything under it. An owner keeps READ_CONTROL and WRITE_DAC
    whatever the DACL says, so a backup item owned by any other account could be re-opened later. Callers run
    Get-CollectorTreeFiles first: icacls /T is never given a tree with a reparse point in it.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $output = & icacls $Path /setowner "*S-1-5-32-544" /T /Q 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Set-CollectorTreeOwner: icacls /setowner failed on '$Path' (exit $LASTEXITCODE): $output"
    }
}

function Test-CollectorOwnerTrusted {
    <# Whether $Path (and, with -Recurse, everything under it) is owned by SYSTEM or Administrators. #>
    param([Parameter(Mandatory)][string]$Path, [switch]$Recurse)

    $paths = @($Path)
    if ($Recurse -and (Test-Path -LiteralPath $Path -PathType Container)) {
        $paths += @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction Stop | ForEach-Object { $_.FullName })
    }
    foreach ($item in $paths) {
        $owner = (Get-Acl -LiteralPath $item).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $Script:CollectorAclAllowedSids) {
            return $false
        }
    }
    return $true
}

function Protect-CollectorBackupTree {
    <#
    Restricts an existing backup tree and proves it: no reparse point anywhere, every item SYSTEM+Administrators
    only (Protect-CollectorPath -Recurse) and owned by Administrators, both read back. Throws on any failure.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $tree = Get-CollectorTreeFiles -Root $Path
    if (@($tree.Problems).Count -gt 0) {
        throw "BACKUP REDIRECTED: $(@($tree.Problems) -join '; ')"
    }
    Protect-CollectorPath -Path $Path -Recurse
    Set-CollectorTreeOwner -Path $Path
    if (-not (Test-CollectorPathProtected -Path $Path -Recurse)) {
        throw "BACKUP NOT PROTECTED: '$Path' did not read back as SYSTEM+Administrators only."
    }
    if (-not (Test-CollectorOwnerTrusted -Path $Path -Recurse)) {
        throw "BACKUP OWNER NOT TRUSTED: an item under '$Path' is not owned by Administrators or SYSTEM."
    }
}

function Complete-ProtectedCollectorBackup {
    <#
    The step after copying into a folder New-ProtectedCollectorDirectory made: restrict and prove the tree
    (Protect-CollectorBackupTree), then compare the copy with $SourceRoot (Test-CollectorBackupCopy) and, with
    -RequireReleaseManifest (a frozen backup), check the release files it records (Test-CollectorRecordedFiles).
    Throws with every problem listed; the caller must then NOT remove or replace the install.
    #>
    param(
        [Parameter(Mandatory)][string]$SourceRoot,
        [Parameter(Mandatory)][string]$BackupRoot,
        [string[]]$RelativePaths,
        [switch]$RequireReleaseManifest
    )

    Protect-CollectorBackupTree -Path $BackupRoot

    $copyArgs = @{ SourceRoot = $SourceRoot; BackupRoot = $BackupRoot }
    if ($PSBoundParameters.ContainsKey("RelativePaths")) {
        $copyArgs.RelativePaths = $RelativePaths
    }
    $problems = @(Test-CollectorBackupCopy @copyArgs)
    if ($RequireReleaseManifest) {
        $problems += @(Test-CollectorRecordedFiles -Root $BackupRoot)
    }
    if ($problems.Count -gt 0) {
        throw "BACKUP FAILED VERIFICATION ($($problems.Count) problem(s)):`n  " + ($problems -join "`n  ")
    }
}

function Get-CollectorTrustedBackup {
    <#
    What tools\restore-backup.ps1 may restore from $BackupRoot, judged ONLY against $TrustedManifestSha256: the
    SHA-256 of the release's MANIFEST.json, obtained outside the backup. Returns Manifest (the parsed, trusted
    MANIFEST.json), Files (the frozen release files, installed_path and sha256 from THAT manifest), Unlisted (files
    in the backup no release placed -- never restored) and Problems (none = restorable). Only frozen releases.
    #>
    param(
        [Parameter(Mandatory)][string]$BackupRoot,
        [Parameter(Mandatory)][string]$TrustedManifestSha256
    )

    $result = [pscustomobject]@{ Manifest = $null; Files = @(); Unlisted = @(); Problems = @() }
    if ($TrustedManifestSha256 -notmatch '^[0-9A-Fa-f]{64}$') {
        $result.Problems = @("TRUSTED HASH MALFORMED: '$TrustedManifestSha256' is not a SHA-256 (64 hexadecimal characters).")
        return $result
    }

    $tree = Get-CollectorTreeFiles -Root $BackupRoot
    if (@($tree.Problems).Count -gt 0) {
        $result.Problems = @($tree.Problems)
        return $result
    }
    if (-not $tree.Files.ContainsKey("MANIFEST.json")) {
        $result.Problems = @("MANIFEST MISSING: the backup has no MANIFEST.json, so it cannot be matched to a trusted release.")
        return $result
    }
    $actual = Get-CollectorFileSha256 $tree.Files["MANIFEST.json"]
    if ($actual -ne $TrustedManifestSha256.ToUpperInvariant()) {
        $result.Problems = @("MANIFEST NOT TRUSTED: the backup's MANIFEST.json has SHA-256 $actual, not the trusted $($TrustedManifestSha256.ToUpperInvariant()) -- " +
                             "it is a different release, or it was altered.")
        return $result
    }

    $manifest = Get-Content -LiteralPath $tree.Files["MANIFEST.json"] -Raw | ConvertFrom-Json
    $layout = Get-CollectorInstalledLayout -Manifest $manifest -BundleKind "frozen"
    $files = @($layout.Files)
    if (@($files | Where-Object { $_.installed_path -eq "SortViewCollector.exe" }).Count -ne 1) {
        $result.Problems = @("NOT A FROZEN RELEASE: the trusted manifest has no runtime/SortViewCollector.exe -- restore-backup.ps1 restores frozen installs only.")
        return $result
    }

    $problems = @()
    $listed = New-Object 'System.Collections.Generic.HashSet[string]' ($Script:CollectorManifestComparer)
    foreach ($name in $Script:CollectorBackupMetadataFiles) { [void]$listed.Add($name) }
    foreach ($entry in $files) {
        if (-not (Test-CollectorManifestPathSafe $entry.installed_path)) {
            $problems += "UNSAFE MANIFEST PATH: $($entry.installed_path)"
            continue
        }
        $relative = ConvertTo-CollectorCanonicalRelativePath $entry.installed_path
        [void]$listed.Add($relative)
        if (-not $tree.Files.ContainsKey($relative)) {
            $problems += "MISSING FROM BACKUP: $relative"
        } elseif ((Get-CollectorFileSha256 $tree.Files[$relative]) -ne $entry.sha256.ToUpperInvariant()) {
            $problems += "ALTERED: $relative differs from the trusted release"
        }
    }

    $result.Manifest = $manifest
    $result.Files = $files
    $result.Unlisted = @(@($tree.Files.Keys) | Where-Object { -not $listed.Contains($_) } | Sort-Object)
    $result.Problems = $problems
    return $result
}
