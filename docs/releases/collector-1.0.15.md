# SortView Collector 1.0.15 Release Notes

## Status

- Release: `1.0.15` (planned)
- `collector.__version__`: `1.0.15`
- Canonical release artifact: **not built.** No bundle, setup program package or hash exists for 1.0.15 yet.
- Implementation: on the working tree, **not committed, merged or released**. Automated validation is recorded in
  `docs/PROJECT_MANIFEST.md` (section 4, C, *NBPL 1.0.13 → 1.0.15 upgrade status*).
- Security tests: `tests/test_collector_backup_security.py` all passed on LIB-L26, 2026-11-16: 40 non-elevated, plus
  3 elevated in 19.33 s, 0 failed, 0 skipped. These are unit and integration tests in temporary folders, **not** an
  upgrade or rollback rehearsal.
- Production: NBPL's sorter (`NEWBRAUNFELSSOR`) runs **1.0.13**. 1.0.15 is **not ready for its upgrade** until a real
  1.0.13 → 1.0.15 upgrade and rollback rehearsal passes (see *Before this release is used for an upgrade*).

1.0.15 supersedes 1.0.14 as the release intended for NBPL's upgrade. 1.0.14's canonical bundle
(`75e7830a…`) and G2 package (`6f91da7d…`) and their records in `docs/releases/collector-1.0.14.md` are unchanged:
their `tools\update.ps1` has the backup-permissions vulnerability described below, so they should not be used to
upgrade an existing installation.

## Why

On NBPL's production sorter (2026-11-16) the update backup `C:\SortView\Collector.backup-20261002-081805` inherited
**Authenticated Users Modify** from `C:\SortView`. `tools\update.ps1` made every backup with a plain copy into the
install's parent and never restricted it, and its printed rollback copied that backup, unverified, into the folder
the SYSTEM task runs. No tampering was observed or established; the finding is that it was possible.

## What changed

Deployment tooling only. The runtime, both wire contracts, the identity algorithm, the privacy boundary and the
data root (configuration, secrets, classification rules, cursors) are unchanged.

### Restricted, verified upgrade backups (`tools\update.ps1`)

- Before the task is touched, the install root must not be reached through a junction or symbolic link. The backup
  path must be free: no folder, file, junction or link, dangling ones included.
- The backup folder is **created already restricted** to SYSTEM and Administrators, so nothing copied into it ever
  inherits the parent's ACL. If a folder appears at that path first, it is refused, not reused.
- After the copy, every item is restricted, owned by Administrators, and read back. The backup is then compared file
  by file with the install, and a frozen backup's recorded release files are checked too. Any problem stops the update
  **before the install is removed or replaced**, with the task left disabled.
- Source-mode backups are restricted and verified the same way, including the `-full` folder of a requirements
  rebuild.
- The frozen install root is recreated restricted, so a folder planted at that path during the update is refused
  rather than reused.

### Verified rollback (`tools\restore-backup.ps1`, new)

It replaces the printed bare-copy rollback. Frozen installs only.

1. It requires an elevated session and exactly one trust anchor from **outside** the backup. The anchor is either
   the release record's *Bundle `MANIFEST.json` SHA-256* (`-ExpectedManifestSha256`), or the `MANIFEST.json` of the
   release's verified canonical ZIP (`-TrustedManifestPath`).
2. **It refuses, changing nothing,** when any of these hold:
   - the backup is redirected or contains a reparse point;
   - any item is not restricted, or is owned by another account;
   - its `MANIFEST.json` is not the trusted one;
   - any release file is missing or differs (an altered backup);
   - it is not a frozen release.

   The backup's own `MANIFEST.installed.json` is never trusted.
3. It disables the task and waits out a run in progress.
4. It moves the current install aside intact, to `<InstallRoot>.replaced-<timestamp>`.
5. It restores **only the verified release files** into an already-restricted folder, and then verifies:
   - the whole installed manifest;
   - the manifest's SHA-256;
   - preflight.
6. Only then does it re-enable the task, and only if the task was enabled before.

Any failure leaves collection **disabled** and prints how to return to the installation that was replaced.

### Bundle

New files: `tools\CollectorBackup.ps1` (the shared library) and `tools\restore-backup.ps1`.
`tools\CollectorManifest.ps1` gains `Get-CollectorInstalledLayout`, the installed-layout definition both
`New-InstalledManifest` and the restore use. Its output is unchanged.

## Compatibility

- `tools\update.ps1` from 1.0.15 now **refuses a frozen install that has no `MANIFEST.json` and
  `MANIFEST.installed.json`**: such a backup could not be verified or restored. NBPL's install has both (verified
  2026-10-22).
- A backup made by an earlier release (NBPL's 2026-10-02 backup included) is not restricted, so
  `tools\restore-backup.ps1` refuses it. Restore such a release from its verified canonical ZIP instead.
- 1.0.13's release record has no *Bundle `MANIFEST.json` SHA-256*. A rollback to 1.0.13 therefore uses
  `-TrustedManifestPath` with the `MANIFEST.json` of `SortViewCollector-1.0.13.zip`, after that ZIP is verified
  against its recorded SHA-256 (`EB389BF6…0450`).

## Before this release is used for an upgrade

To be performed and recorded here:

- [ ] Build the canonical 1.0.15 bundle, and record its ZIP, `MANIFEST.json` and executable SHA-256 and its artifact
      storage.
- [x] Run the three tests of `tests/test_collector_backup_security.py` that skip without elevation, in an elevated
      session on LIB-L26. **Passed 2026-11-16: 3 passed in 19.33 s, 0 failed, 0 skipped** (Windows PowerShell 5.1
      elevated, Python 3.11.9, pytest 8.4.2). The rest of the file had passed non-elevated: 40 passed.
  - [x] the symbolic-link redirect test;
  - [x] the real restricted backup in a parent granting Authenticated Users Modify, with the source install left
        unchanged;
  - [x] the rollback that fails after moving the install aside: the original stayed intact in `.replaced-*`, the task
        was not re-enabled, and the printed recovery worked.
- [ ] **A 1.0.13 → 1.0.15 upgrade rehearsal** on a VM reproducing the canonical NBPL profile:
  - [ ] a 1.0.13 Contract v2 install at the same paths;
  - [ ] the SYSTEM task at run level Highest;
  - [ ] `C:\SortView` granting Authenticated Users Modify;
  - [ ] parser logs under the install root;
  - [ ] the backup comes out restricted and Administrators-owned;
  - [ ] the configuration, secrets, rules and cursors are unchanged;
  - [ ] the installed manifest verifies;
  - [ ] the SYSTEM preflight passes;
  - [ ] a scheduled run uploads with no gap or duplicate.
- [ ] **A rollback rehearsal** with `tools\restore-backup.ps1` to 1.0.13:
  - [ ] it refuses an unrestricted, altered, redirected or wrong-anchor backup, changing nothing;
  - [ ] it restores a good backup;
  - [ ] after a forced preflight failure, it leaves the task disabled and its recovery instructions work.
- [ ] Code signing (C1) remains open. The bundle is unsigned.
