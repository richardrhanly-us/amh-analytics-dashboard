<#
.SYNOPSIS
    Shared least-privilege ACL helper for the collector's OWN install/data
    trees -- SYSTEM and Administrators only, everyone else denied.

.DESCRIPTION
    This is the reusable primitive Item 3 of the pre-cutover work asked
    for: install.ps1, update.ps1 and repair-collector-permissions.ps1 all
    dot-source this ONE file rather than each carrying its own icacls
    calls. It intentionally reuses the SAME approach and the SAME two
    well-known SIDs already used elsewhere in this codebase for an
    identical purpose:

      - collector/v2_keys.py's protect_directory()/acl_state_of() (Python,
        icacls) locks the v2 DPAPI secret folder to SYSTEM+Administrators
        and re-verifies it by reading the DACL back on every run.
      - collector/deploy/setup-collector.ps1's Protect-SetupRecoveryDirectory
        (PowerShell, .NET DirectorySecurity) locks the setup-resume folder
        the same way.

    Neither of those touches -InstallRoot, -InstallRoot\logs, or
    -DataRoot\{config,data,logs} -- which is exactly the confirmed onsite
    gap: "Authenticated Users: Modify" inherited on C:\SortView\Collector
    (and the .exe and its logs\ underneath it, simply by being children of
    an unprotected parent), and "BUILTIN\Users: Write" at the
    C:\ProgramData\SortViewCollector and \config directory level. Nothing
    in this repository has ever set an ACL on any of those paths before.

    This file NEVER touches the v2 secrets directory
    (-DataRoot\secrets\v2_key.dpapi and its folder) -- that stays governed
    exclusively by v2_keys.py's own, separately fail-closed logic, per the
    explicit instruction that it "remains governed by the existing
    stricter v2 secret ACL logic." Callers must simply never pass that
    path to the functions below.

    Uses icacls (like v2_keys.py), not Protect-SetupRecoveryDirectory's
    .NET DirectorySecurity approach, specifically because -Recurse below
    needs to correct an ALREADY-POPULATED tree -- an inherited or explicit
    ACE on an existing child file/folder is not retroactively rewritten
    just because the parent's ACL changes on its own. A brand-new,
    still-empty directory (the setup-recovery folder, the v2 secrets
    folder) never had this problem in the first place, which is why
    neither of those existing helpers needed it.

    TWO CONFIRMED, NON-OBVIOUS icacls PITFALLS this implementation works
    around (found by reproducing the exact onsite ACE shape against a real
    disposable directory, not assumed):

      1. icacls's own /T recurses by re-running ONE literal grant clause
         against every enumerated item regardless of type. Directories
         need (OI)(CI) (so future children inherit); a plain FILE given
         those same flags does not error -- icacls reports success -- but
         silently ends up with an EMPTY ACL (zero access rules), which
         would break even SYSTEM's own access to it. So grants are applied
         per item (Protect-CollectorPathSingle), with (OI)(CI) only for an
         actual container, never via /T on the grant step itself.
      2. /grant:r only ever replaces the NAMED principal's own grant -- it
         never removes a different principal's pre-existing EXPLICIT
         grant, so a file that already carries "Authenticated Users:
         Modify" (the exact onsite shape) would keep it sitting right
         alongside a new SYSTEM/Administrators grant. An unconditional
         /reset (recursively, where requested) runs first to wipe every
         explicit ACE and restore pure inheritance, giving the per-item
         grant step a genuinely clean slate.
#>

Set-StrictMode -Version Latest

$Script:CollectorAclSystemSid = "S-1-5-18"
$Script:CollectorAclAdministratorsSid = "S-1-5-32-544"
$Script:CollectorAclAllowedSids = @($Script:CollectorAclSystemSid, $Script:CollectorAclAdministratorsSid)

function Protect-CollectorPath {
    <#
    Restricts $Path to SYSTEM and Administrators FullControl only --
    inheritance from its parent cut, well-known SIDs (language
    independent) -- then immediately reads the result back and throws if
    it is not EXACTLY that. Never leaves an unverified path: a caller that
    does not catch the exception must treat this as install/update/repair
    having failed.

    -Recurse (directories only) also re-protects every file and subfolder
    ALREADY inside $Path, not just $Path itself -- required to actually
    correct a tree that already has broader inherited OR explicit grants
    on existing children, which is exactly today's confirmed onsite state
    for C:\SortView\Collector (the .exe and logs\ underneath it) and
    C:\ProgramData\SortViewCollector\{config,data,logs}. Omit -Recurse for
    a directory you have just created yourself and that is still empty
    (nothing to correct yet, and every FUTURE child will inherit the
    now-correct ACL automatically) -- both callers below use it that way.

    Fails closed: any icacls failure, or a post-application verification
    that does not read back as fully protected, throws rather than
    returning a status the caller might ignore.
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$Recurse
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Protect-CollectorPath: '$Path' does not exist -- nothing to protect."
    }

    $isRecursiveContainer = $Recurse -and (Test-Path -LiteralPath $Path -PathType Container)
    $recurseFlag = if ($isRecursiveContainer) { @("/T") } else { @() }

    # STEP 1: wipe every existing EXPLICIT ACE (including an unrelated principal's pre-existing grant --
    # the confirmed onsite shape, e.g. "Authenticated Users: Modify") and restore pure inheritance,
    # recursively if requested. /reset takes no rights/flags, so it behaves correctly for files and
    # directories alike -- proven necessary because /grant:r below only ever replaces the NAMED
    # principal's own grant; it never removes a different principal's existing explicit one, so without
    # this step a file that already carries "Authenticated Users: Modify" keeps it sitting right
    # alongside the new SYSTEM/Administrators grant instead of losing it.
    $resetArgs = @($Path, "/reset") + $recurseFlag
    $resetOutput = & icacls @resetArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Protect-CollectorPath: icacls /reset failed on '$Path' (exit $LASTEXITCODE): $resetOutput"
    }

    # STEP 2: grant SYSTEM+Administrators, applied to $Path itself and (with -Recurse) to every existing
    # child INDIVIDUALLY -- never via icacls's own /T on the grant clause. Confirmed by direct
    # reproduction: (OI)(CI) (container/object-inherit) flags given directly to a plain FILE make icacls
    # silently produce an EMPTY ACL (exit code 0, "successfully processed", but zero access rules left --
    # not documented, not an error, genuinely dangerous, since a file left with no SYSTEM grant at all
    # would break the very Scheduled Task this exists to protect). /T reapplies one literal grant clause
    # to every enumerated item regardless of type, so a mixed file+directory tree hits this on every file.
    # Applying per-item, with (OI)(CI) only for an actual container, avoids it entirely.
    Protect-CollectorPathSingle -Path $Path
    if ($isRecursiveContainer) {
        # -ErrorAction Stop: Get-ChildItem's own access-denied on a partially-locked tree is, by PowerShell
        # default, a NON-terminating error -- silently producing zero items rather than stopping. Without this,
        # an enumeration failure here would make the pipeline process NO children at all while still reporting
        # overall success, exactly the false-positive this function exists to prevent (confirmed by direct
        # reproduction: a top-down lockout mid-recursion made this silently "succeed" having fixed nothing).
        Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction Stop |
            ForEach-Object { Protect-CollectorPathSingle -Path $_.FullName }
    }

    if (-not (Test-CollectorPathProtected -Path $Path -Recurse:$Recurse)) {
        throw "Protect-CollectorPath: '$Path' was not left protected (SYSTEM+Administrators only, inheritance cut) after icacls reported success -- refusing to proceed with an unverified ACL."
    }
}

function Protect-CollectorPathSingle {
    # Internal: STEP 2 of Protect-CollectorPath for exactly one file or directory -- never called with
    # -Recurse itself, and never given (OI)(CI) flags unless $Path is actually a container.
    param([Parameter(Mandatory)][string]$Path)

    $isContainer = Test-Path -LiteralPath $Path -PathType Container
    $grants = $Script:CollectorAclAllowedSids | ForEach-Object { if ($isContainer) { "*${_}:(OI)(CI)F" } else { "*${_}:F" } }
    $output = & icacls $Path /inheritance:r /grant:r @grants 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Protect-CollectorPath: icacls failed to protect '$Path' (exit $LASTEXITCODE): $output"
    }
}

function Test-CollectorPathProtected {
    <#
    Reads $Path's ACL back (Get-Acl, not icacls output) and returns $true
    only if it is EXACTLY the intended policy -- see Test-OneCollectorAclEntry
    for the precise rule: inheritance cut, Allow/FullControl for SYSTEM,
    Allow/FullControl for Administrators, no other trustee, no Deny rule
    at all, nothing weaker than FullControl for either trustee. With
    -Recurse (directories only), every existing child (file and subfolder,
    at any depth) must independently satisfy the same policy -- a
    directory whose OWN ACL is correct but that still has one broadly-
    permissioned child underneath it (a leftover Deny, an extra trustee, a
    weaker right, or simply still-inherited) reads as NOT protected.

    Never throws for an ACL that simply is not protected -- returns
    $false. Only a path that cannot be read AT ALL (Get-Acl itself
    failing, or Get-ChildItem unable to even enumerate $Path's children)
    is allowed to propagate as an error, since that is a real "cannot
    verify" condition, not a "verified and it's wrong" one -- and,
    critically, NOT the same as "no children exist": Get-ChildItem's own
    access-denied is a non-terminating error by default, silently
    producing zero items rather than stopping, which would otherwise make
    an unreadable subtree read as "protected" by simply never being
    checked. -ErrorAction Stop below turns that into a real, propagated
    error instead.
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$Recurse
    )

    if (-not (Test-OneCollectorAclEntry -Path $Path)) {
        return $false
    }

    if ($Recurse -and (Test-Path -LiteralPath $Path -PathType Container)) {
        $children = Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        foreach ($child in $children) {
            if (-not (Test-OneCollectorAclEntry -Path $child.FullName)) {
                return $false
            }
        }
    }

    return $true
}

function Test-OneCollectorAclEntry {
    <#
    Internal: the single-path (non-recursive) check Test-CollectorPathProtected builds on. Passes ONLY when
    $Path's DACL, read back via Get-Acl (never a string parse of icacls's own text output, which is locale-
    and format-dependent), is EXACTLY the intended policy:

      - AreAccessRulesProtected (inheritance cut)
      - exactly one Allow/FullControl rule for SYSTEM (S-1-5-18)
      - exactly one Allow/FullControl rule for Administrators (S-1-5-32-544)
      - no rule for any other trustee
      - no Deny rule at all, for ANY trustee, including SYSTEM/Administrators themselves
      - no rule granting anything less than FullControl for either trustee (ReadAndExecute, Modify, etc. are
        all strict subsets and must fail this check, not be mistaken for "close enough")

    FullControl comparison uses a bitmask (-band), not equality, deliberately: FullControl's own defined value
    already includes the Synchronize bit, and NTFS/icacls can represent the identical effective "full control"
    grant with that bit toggled depending on exactly how the ACE was constructed. A bitmask check accepts that
    real-world variance while still rejecting anything that is not a full superset of FullControl's own bits --
    a weaker right (a strict subset) fails the AND-equals-FullControl comparison correctly.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        return $false
    }

    $fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
    $seen = @{}

    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
            return $false  # any Deny rule at all, for any trustee, fails closed
        }
        if (($rule.FileSystemRights -band $fullControl) -ne $fullControl) {
            return $false  # anything less than a full superset of FullControl's bits fails closed
        }

        $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $Script:CollectorAclAllowedSids) {
            return $false  # an unexpected trustee
        }
        if ($seen.ContainsKey($sid)) {
            return $false  # a second/split rule for a trustee we already accepted is not the exact policy
        }
        $seen[$sid] = $true
    }

    # Every rule present passed the per-rule checks above; this still confirms neither expected trustee is
    # simply MISSING (e.g. only SYSTEM present, Administrators absent entirely -- zero rules for it, so the
    # loop above never had a chance to reject anything, but the policy is still not met).
    return $seen.Count -eq $Script:CollectorAclAllowedSids.Count
}
