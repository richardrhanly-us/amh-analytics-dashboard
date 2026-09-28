<#
.SYNOPSIS
    Persists the release bundle's already-verified MANIFEST.json into the
    live install, transformed to the INSTALLED layout, and re-verifies it
    later -- Item 4 of the pre-cutover work.

.DESCRIPTION
    install.ps1 and update.ps1 already run Test-ReleaseManifest against
    the BUNDLE before touching anything (every listed file present with
    the correct SHA-256, no unlisted extra file) -- but neither ever
    copies MANIFEST.json into -InstallRoot, so once the bundle folder is
    deleted (the normal case after an install), there is no persistent,
    on-machine record of what was actually installed or its expected
    hashes.

    The bundle's MANIFEST.json paths are BUNDLE-relative and mix three
    different things: files that get installed to -InstallRoot, files
    that stay in the bundle folder only as operator tooling
    (install.ps1/update.ps1/tools/*), and -- for a frozen bundle -- a
    "runtime/" path PREFIX that install.ps1/update.ps1 both STRIP when
    copying (runtime/SortViewCollector.exe becomes
    -InstallRoot/SortViewCollector.exe, not
    -InstallRoot/runtime/SortViewCollector.exe). Comparing the raw bundle
    manifest against the installed tree, path-for-path, would therefore
    either report every real file as "missing" (frozen) or report every
    non-installed tooling file as "missing" too (source and frozen alike)
    -- wrong either way.

    New-InstalledManifest is the ONE place that knows the transform,
    because it is applied at the exact moment install.ps1/update.ps1 know
    with certainty what they just copied and from where -- not
    re-derived/guessed later by a second script working from a name
    pattern. It writes two files to -InstallRoot:

      - MANIFEST.json: an EXACT, unmodified copy of the bundle's own
        manifest (paths untouched) -- the record of what release this
        came from.
      - MANIFEST.installed.json: the derived, INSTALLED-layout view
        Test-InstalledManifest actually verifies against -- one entry per
        file that was really copied to -InstallRoot, its transformed
        path, and its expected SHA-256 (copied straight from the bundle
        manifest's own entry -- this file never (re)computes a hash of
        its own for a file it did not just place there itself), plus the
        "zones" (which subtrees of -InstallRoot are release-managed) so
        Test-InstalledManifest can find an unexpected extra file without
        needing its own copy of this same bundle-kind-specific knowledge.

    Deliberately never lists, and Test-InstalledManifest deliberately
    never scans: -DataRoot (config/, data/, logs/, secrets/) or
    -InstallRoot/.venv//.deps-hash (a SOURCE install's dependencies,
    pip-installed, never a bundle file) -- none of that is a release
    artifact, so none of it belongs in an integrity check against the
    release manifest.

    MANIFEST.json and MANIFEST.installed.json themselves are NEVER listed
    as "files" entries and are explicitly, by name, excluded from the
    unexpected-file scan below (they would otherwise appear to describe/
    verify themselves) -- they are installer-written metadata, not
    release payload, so they do not belong in either role. This is a
    deliberate choice, not an oversight, and is pinned by
    tests/test_collector_acl_and_manifest.py.

.NOTES
    CROSS-PLATFORM PATH HANDLING (this file is exercised by pytest on
    both Windows and Linux runners, via pwsh; only actual production
    installs are Windows-only):

    collector/build_release.py always writes MANIFEST.json paths with
    forward slashes -- Path.as_posix() for the frozen runtime's entries,
    and literal "tools/..."-style string constants for every other entry
    (grep the DEPLOY_TOOL_FILES/SUPPORT_FILES tuples: never a backslash).
    So "/" is this file's ONE canonical relative-path separator, matching
    the data it actually receives, rather than converting everything to
    "\" and then needing every filesystem-discovered path (Get-ChildItem's
    .FullName, which uses "\" on Windows and "/" on Linux) to somehow
    agree with that choice too.

    ConvertTo-CollectorCanonicalRelativePath is the ONE function that
    defines this canonical form -- forward slashes, no leading slash,
    ORIGINAL CASE PRESERVED. Every relative path this file handles, from
    either source (a manifest entry or a discovered file), is passed
    through it before being compared or used as a dictionary key. Case
    folding is handled separately, by the dictionary's own StringComparer
    (OrdinalIgnoreCase on Windows, matching real NTFS case-insensitivity;
    Ordinal on Linux, matching a real case-sensitive filesystem) -- never
    baked into the canonical string itself, so a genuinely
    case-distinct pair of files on Linux is never silently treated as
    the same key.
#>

Set-StrictMode -Version Latest

# True on Windows (backslash is the platform separator), false everywhere else. [System.IO.Path]::DirectorySeparatorChar
# works identically on Windows PowerShell 5.1 and PowerShell 7+ (pwsh, including on Linux) -- unlike the $IsWindows
# automatic variable, which does not exist in Windows PowerShell 5.1 and would error under Set-StrictMode.
$Script:CollectorManifestIsWindows = ([System.IO.Path]::DirectorySeparatorChar -eq '\')
$Script:CollectorManifestComparer = if ($Script:CollectorManifestIsWindows) {
    [System.StringComparer]::OrdinalIgnoreCase
} else {
    [System.StringComparer]::Ordinal
}

function ConvertTo-CollectorCanonicalRelativePath {
    <#
    The ONE canonical relative-path representation this file compares in: forward-slash separators, no leading
    slash, ORIGINAL CASE PRESERVED (case is handled by the caller's choice of StringComparer, not here -- see
    $Script:CollectorManifestComparer). Idempotent: canonicalizing an already-canonical path is a no-op.
    #>
    param([Parameter(Mandatory)][string]$RelativePath)
    return ($RelativePath -replace '\\', '/').TrimStart('/')
}

function Get-CollectorRelativePath {
    <#
    $FullPath's path relative to $Root, canonicalized. Deliberately NOT [System.IO.Path]::GetRelativePath --
    that method does not exist in .NET Framework, which Windows PowerShell 5.1 (still a real production target
    for this tooling) runs on. Deliberately NOT a fixed-length Substring/TrimStart('\') either -- that is only
    correct when $Root's own separator convention matches $FullPath's, which silently breaks the moment either
    side uses "/" instead of "\" (confirmed root cause of a real CI failure on a Linux pwsh runner, where
    Get-ChildItem's .FullName uses "/" throughout).
    #>
    param([Parameter(Mandatory)][string]$Root, [Parameter(Mandatory)][string]$FullPath)

    $canonicalRoot = (ConvertTo-CollectorCanonicalRelativePath $Root).TrimEnd('/')
    $canonicalFull = ($FullPath -replace '\\', '/')
    if ($canonicalFull.Length -lt $canonicalRoot.Length -or
        -not $canonicalFull.Substring(0, $canonicalRoot.Length).Equals($canonicalRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Get-CollectorRelativePath: '$FullPath' is not under '$Root'."
    }
    return $canonicalFull.Substring($canonicalRoot.Length).TrimStart('/')
}

function Test-CollectorManifestPathSafe {
    # Internal: the same traversal/rooted-path guard Test-ReleaseManifest applies to a
    # bundle manifest path, reused here for an installed-manifest path read back later --
    # an installed-manifest entry is installer-generated (trusted), but is still checked,
    # since a malformed/tampered installed-manifest is one of the explicit test cases.
    # Already separator-agnostic (checks both \ and /), so no change needed for portability.
    param([Parameter(Mandatory)][string]$RelativePath)
    return -not ($RelativePath -match '(^|[\\/])\.\.([\\/]|$)' -or $RelativePath -match '^[\\/]' -or $RelativePath -match '^[A-Za-z]:')
}

function New-InstalledManifest {
    <#
    Writes -InstallRoot/MANIFEST.json (unmodified copy of the bundle's)
    and -InstallRoot/MANIFEST.installed.json (the installed-layout view),
    from a $Manifest already parsed and verified by Test-ReleaseManifest.
    $BundleKind is "frozen" or "source" -- must match the branch the
    caller actually took when copying files, not re-detected here.
    #>
    param(
        [Parameter(Mandatory)][string]$BundleRoot,
        [Parameter(Mandatory)][string]$InstallRoot,
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)][ValidateSet("frozen", "source")][string]$BundleKind
    )

    $installed = @()
    $zones = @()

    if ($BundleKind -eq "frozen") {
        $runtimePrefix = "runtime/"
        foreach ($entry in $Manifest.files) {
            $canonical = ConvertTo-CollectorCanonicalRelativePath $entry.path
            if ($canonical.StartsWith($runtimePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                $installedPath = $canonical.Substring($runtimePrefix.Length)
                $installed += [ordered]@{ installed_path = $installedPath; bundle_path = $entry.path; sha256 = $entry.sha256.ToUpper() }
            }
        }
        # The whole InstallRoot IS the frozen runtime -- nothing else belongs there.
        $zones += [ordered]@{ root = ""; recurse = $true }
    } else {
        foreach ($entry in $Manifest.files) {
            $canonical = ConvertTo-CollectorCanonicalRelativePath $entry.path
            $isTopLevelCollectorPy = ($canonical -match '^collector/[^/]+\.py$')
            $isAgentFile = $canonical.StartsWith("agent/", [System.StringComparison]::OrdinalIgnoreCase)
            if ($isTopLevelCollectorPy -or $isAgentFile) {
                $installed += [ordered]@{ installed_path = $canonical; bundle_path = $entry.path; sha256 = $entry.sha256.ToUpper() }
            }
        }
        # collector/ is copied top-level-*.py-only (no subfolder); agent/ is copied whole and recursively.
        $zones += [ordered]@{ root = "collector"; recurse = $false }
        $zones += [ordered]@{ root = "agent"; recurse = $true }
    }

    $installedManifest = [ordered]@{
        bundle_kind   = $BundleKind
        bundle_product = $Manifest.product
        bundle_version = $Manifest.version
        bundle_built_at = $Manifest.built_at
        installed_at  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ")
        zones         = $zones
        files         = $installed
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $bundleManifestPath = Join-Path $BundleRoot "MANIFEST.json"
    [System.IO.File]::WriteAllText((Join-Path $InstallRoot "MANIFEST.json"), (Get-Content $bundleManifestPath -Raw), $utf8NoBom)
    [System.IO.File]::WriteAllText((Join-Path $InstallRoot "MANIFEST.installed.json"), ($installedManifest | ConvertTo-Json -Depth 6), $utf8NoBom)

    Write-Host "Persisted MANIFEST.json and MANIFEST.installed.json ($($installed.Count) installed file(s) recorded) to $InstallRoot" -ForegroundColor Green
}

function Test-InstalledManifest {
    <#
    Read-only. Reads -InstallRoot/MANIFEST.installed.json, recomputes the
    SHA-256 of every recorded file and compares, then scans each recorded
    "zone" for a file that exists on disk but is not in the recorded list.
    Prints PASS or the itemized problem list, then throws if there was
    any problem (same convention as Test-ReleaseManifest). Never modifies
    -InstallRoot.

    Every relative path -- recorded (installed_path) or discovered
    (Get-ChildItem) -- goes through ConvertTo-CollectorCanonicalRelativePath
    before comparison, and the comparison itself uses
    $Script:CollectorManifestComparer (case-insensitive on Windows,
    case-sensitive on Linux) -- see this file's .NOTES for why neither of
    those is optional.
    #>
    param([Parameter(Mandatory)][string]$InstallRoot)

    $installedManifestPath = Join-Path $InstallRoot "MANIFEST.installed.json"
    if (-not (Test-Path $installedManifestPath -PathType Leaf)) {
        throw "MANIFEST MISSING: no MANIFEST.installed.json at '$InstallRoot' -- this install predates release-integrity persistence, or -InstallRoot is wrong. Nothing was checked."
    }

    try {
        $installedManifest = Get-Content $installedManifestPath -Raw | ConvertFrom-Json
    } catch {
        throw "MALFORMED MANIFEST: '$installedManifestPath' is not valid JSON ($($_.Exception.Message)). Nothing was checked."
    }

    if (-not $installedManifest.files -or -not $installedManifest.zones) {
        throw "MALFORMED MANIFEST: '$installedManifestPath' is missing its 'files' or 'zones' list. Nothing was checked."
    }

    $problems = @()
    $recordedKeys = New-Object 'System.Collections.Generic.Dictionary[string,bool]' ($Script:CollectorManifestComparer)

    foreach ($entry in $installedManifest.files) {
        if (-not (Test-CollectorManifestPathSafe $entry.installed_path)) {
            $problems += "UNSAFE MANIFEST PATH: $($entry.installed_path) (traversal or rooted/absolute path) -- skipped, not checked against disk"
            continue
        }
        $canonical = ConvertTo-CollectorCanonicalRelativePath $entry.installed_path
        $recordedKeys[$canonical] = $true

        # Join-Path with a forward-slash-separated ChildPath produces a working path on both platforms: Win32/.NET
        # file APIs accept "/" interchangeably with "\", and it is already native on Linux. No extra conversion needed.
        $filePath = Join-Path $InstallRoot $entry.installed_path
        if (-not (Test-Path $filePath -PathType Leaf)) {
            $problems += "MISSING: $($entry.installed_path) (bundle path: $($entry.bundle_path))"
            continue
        }
        $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash
        if ($actualHash -ne $entry.sha256.ToUpper()) {
            $problems += "HASH MISMATCH: $($entry.installed_path) (expected $($entry.sha256), got $($actualHash.ToLower()))"
        }
    }

    foreach ($zone in $installedManifest.zones) {
        $zoneRoot = if ([string]::IsNullOrEmpty($zone.root)) { $InstallRoot } else { Join-Path $InstallRoot $zone.root }
        if (-not (Test-Path -LiteralPath $zoneRoot)) { continue }
        $zoneFiles = if ($zone.recurse) {
            Get-ChildItem -LiteralPath $zoneRoot -Recurse -File
        } else {
            Get-ChildItem -LiteralPath $zoneRoot -File
        }
        foreach ($f in $zoneFiles) {
            $relative = Get-CollectorRelativePath -Root $InstallRoot -FullPath $f.FullName
            # Exact-name check, not canonicalized/case-folded: we control the exact casing of both files
            # ourselves (New-InstalledManifest always writes these two literal names), so there is no
            # platform-dependent case question to resolve here -- see this file's .DESCRIPTION.
            if ($relative -in @("MANIFEST.json", "MANIFEST.installed.json")) { continue }
            $canonical = ConvertTo-CollectorCanonicalRelativePath $relative
            if (-not $recordedKeys.ContainsKey($canonical)) {
                $problems += "UNEXPECTED FILE (not in MANIFEST.installed.json): $relative"
            }
        }
    }

    if ($problems.Count -gt 0) {
        Write-Host "INSTALLED-MANIFEST VERIFICATION FAILED for '$InstallRoot':" -ForegroundColor Red
        foreach ($p in $problems) { Write-Host "  $p" -ForegroundColor Red }
        throw "Refusing to report success -- $($problems.Count) installed-manifest problem(s) at '$InstallRoot'."
    }

    Write-Host "PASS: $($installedManifest.files.Count) installed file(s) match MANIFEST.installed.json, no unexpected file in a release-managed zone." -ForegroundColor Green
}
