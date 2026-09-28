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

    The bundle's MANIFEST.json paths are BUNDLE-relative (e.g.
    "collector\run.py", "runtime\SortViewCollector.exe",
    "tools\install.ps1") and mix three different things: files that get
    installed to -InstallRoot, files that stay in the bundle folder only
    as operator tooling (install.ps1/update.ps1/tools\*), and -- for a
    frozen bundle -- a "runtime\" path PREFIX that install.ps1/update.ps1
    both STRIP when copying (runtime\SortViewCollector.exe becomes
    -InstallRoot\SortViewCollector.exe, not
    -InstallRoot\runtime\SortViewCollector.exe). Comparing the raw bundle
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
    never scans: -DataRoot (config\, data\, logs\, secrets\) or
    -InstallRoot\.venv\/.deps-hash (a SOURCE install's dependencies,
    pip-installed, never a bundle file) -- none of that is a release
    artifact, so none of it belongs in an integrity check against the
    release manifest.
#>

Set-StrictMode -Version Latest

function ConvertTo-CollectorRelativeKey {
    # Internal: the same normalization Test-ReleaseManifest uses for a manifest path
    # (backslash-normalized, lower-invariant) so keys compare consistently everywhere.
    param([Parameter(Mandatory)][string]$RelativePath)
    return ($RelativePath -replace '/', '\').ToLowerInvariant()
}

function Test-CollectorManifestPathSafe {
    # Internal: the same traversal/rooted-path guard Test-ReleaseManifest applies to a
    # bundle manifest path, reused here for an installed-manifest path read back later --
    # an installed-manifest entry is installer-generated (trusted), but is still checked,
    # since a malformed/tampered installed-manifest is one of the explicit test cases.
    param([Parameter(Mandatory)][string]$RelativePath)
    return -not ($RelativePath -match '(^|[\\/])\.\.([\\/]|$)' -or $RelativePath -match '^[\\/]' -or $RelativePath -match '^[A-Za-z]:')
}

function New-InstalledManifest {
    <#
    Writes -InstallRoot\MANIFEST.json (unmodified copy of the bundle's)
    and -InstallRoot\MANIFEST.installed.json (the installed-layout view),
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
        foreach ($entry in $Manifest.files) {
            # Match case-insensitively (the same normalization Test-ReleaseManifest itself uses), but strip the
            # "runtime\" prefix from the ORIGINAL, case-preserved path -- not from the lower-invariant match key --
            # so installed_path keeps the real on-disk casing (e.g. "SortViewCollector.exe", not "sortviewcollector.exe").
            $normalizedPath = $entry.path -replace '/', '\'
            $key = ConvertTo-CollectorRelativeKey $entry.path
            if ($key -like "runtime\*") {
                $installedPath = $normalizedPath.Substring("runtime\".Length)
                $installed += [ordered]@{ installed_path = $installedPath; bundle_path = $entry.path; sha256 = $entry.sha256.ToUpper() }
            }
        }
        # The whole InstallRoot IS the frozen runtime -- nothing else belongs there.
        $zones += [ordered]@{ root = ""; recurse = $true }
    } else {
        foreach ($entry in $Manifest.files) {
            $key = ConvertTo-CollectorRelativeKey $entry.path
            $isTopLevelCollectorPy = ($key -match '^collector\\[^\\]+\.py$')
            $isAgentFile = $key -like "agent\*"
            if ($isTopLevelCollectorPy -or $isAgentFile) {
                $installed += [ordered]@{ installed_path = $entry.path; bundle_path = $entry.path; sha256 = $entry.sha256.ToUpper() }
            }
        }
        # collector\ is copied top-level-*.py-only (no subfolder); agent\ is copied whole and recursively.
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
    Read-only. Reads -InstallRoot\MANIFEST.installed.json, recomputes the
    SHA-256 of every recorded file and compares, then scans each recorded
    "zone" for a file that exists on disk but is not in the recorded list.
    Prints PASS or the itemized problem list, then throws if there was
    any problem (same convention as Test-ReleaseManifest). Never modifies
    -InstallRoot.
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
    $recordedKeys = @{}

    foreach ($entry in $installedManifest.files) {
        if (-not (Test-CollectorManifestPathSafe $entry.installed_path)) {
            $problems += "UNSAFE MANIFEST PATH: $($entry.installed_path) (traversal or rooted/absolute path) -- skipped, not checked against disk"
            continue
        }
        $key = ConvertTo-CollectorRelativeKey $entry.installed_path
        $recordedKeys[$key] = $true

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
            $relative = $f.FullName.Substring($InstallRoot.Length).TrimStart('\')
            if ($relative -in @("MANIFEST.json", "MANIFEST.installed.json")) { continue }
            $key = ConvertTo-CollectorRelativeKey $relative
            if (-not $recordedKeys.ContainsKey($key)) {
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
