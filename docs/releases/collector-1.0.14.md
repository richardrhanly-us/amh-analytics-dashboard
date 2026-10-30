# SortView Collector 1.0.14 Release Notes

## Status

- Release: `1.0.14`
- `collector.__version__`: `1.0.14`
- Canonical release artifact: **built 2026-10-24, not yet published** (see *Canonical release artifact* below). The
  remaining checks in *Before this release is published* are still to be performed and recorded.
- G3 real-API GUI installation: **passed 2026-10-25** (see *G3 validation: real-API GUI installation* below).
- G2 setup program package: the unsigned supervised-pilot package (see *G2 setup program package* below).
  Exact-package GUI installation **passed 2026-10-29** (see *G2 validation: exact-package GUI installation* below).
- Artifact storage: the canonical release ZIP and the G2 package were recovered and stored in the private artifact
  repository on 2026-10-28. Storage is internal and is not release or customer approval.
- Customer documentation: **written 2026-10-26** (G6B) -- `docs/collector-1.0.14-customer-installation-guide.md` and the
  internal `docs/customer-onboarding-checklist.md`, replacing the 1.0.2 guide and checklist. Not yet used for a
  production onboarding.

## Canonical release artifact

Built from `62a4ae8f96728c281ce0575589850959c9886efc` (PRs #265 and #266) with `collector/freeze/build_frozen.ps1` and
`python -m collector.build_release`, then compressed and verified as an extracted copy:

- Release ZIP: `SortViewCollector-1.0.14.zip` (36,543,962 bytes)
- Release ZIP SHA-256: `75e7830affaad61cc1c5716996287b9d8115a802087d6e25b63da9c132e501f5`
- Bundle `MANIFEST.json` SHA-256: `8758e0f6dc86f288d94e013ad1540b8f95ea9675efb5514899c5898463339bb4`
- Manifest entries: `1392`; frozen runtime files: `1378`
- `runtime\SortViewCollector.exe` SHA-256: `e7c733a9c622fd9c2bf244b29d9cf2c264e323ab53cf72fa759c1298f7691399`
- Build: CPython 3.14.6, PyInstaller 6.22.3; requests 2.33.1, urllib3 2.6.3, pandas 2.3.3, numpy 2.5.3
- Stored: `sortview-artifacts` `collector/1.0.14/SortViewCollector-1.0.14.zip`, commit `0bf29645868024556df4225c0a0367d42332558b`

Earlier test builds also report `1.0.14` but are **not** this release: the frozen runtime used in the M2d-4 P0C/P1/P1R
VM tests (`SortViewCollector.exe` SHA-256 `9b948e8266748c0aa2bea074575bcb7c945c46b587a988ccb8bdd88072d81040`) predates
PR #266. Identify a 1.0.14 artifact by the hashes above, never by its version output.

The setup program for this bundle is packaged with `scripts/package_installer_release.py` (see
`docs/release-process.md`, *SortView setup program package*), which takes the two SHA-256 values above from this record.

1.0.14 is the first Collector release whose guided setup can install a **new** machine directly on
Contract v2. **Contract 2 is not yet what a newly provisioned installation gets:** Super Admin still
creates a contract 1 installation unless an administrator selects Contract v2 explicitly. Making it the
default is a later, separate change, made only after this bundle has been rehearsed and published.

## G3 validation: real-API GUI installation

Passed on 2026-10-25. The setup program package was installed with the GUI on a fresh machine against the deployed
SortView API.

- Environment: fresh Windows 10 Enterprise LTSC 2019 VM (`SortView-G3-RealAPI-Install`), VMware Workstation NAT
- Setup program package: built from `de858df`; package ZIP SHA-256
  `e932200f63c2f3179cc2b19e7dd9cbe69836cb5d54002de7553db41be27b7659`; Collector `1.0.14`
- API: the deployed SortView API over publicly trusted HTTPS
- Test organization: `SortView G3 Installer Test`, branch `G3 Test Branch`, installation ID `7`, Contract v2
- GUI prerequisite checks passed; bundle verification passed for all 1,392 manifest files
- GUI installation completed all seven stages, exit code 0
- Scheduled Task `SortView Collector` (SYSTEM) enabled; its SYSTEM run ended with LastTaskResult 0
- Collector health: healthy, no errors
- Synthetic Tech Logic data (7 ACS events, 3 check-ins, 1 reject) reached the real API: 11 events inserted in 3
  batches, 3 chunks committed, 0 server duplicates, 0 quarantined events
- Backend: installation active, onboarding Ready, pipeline OK
- Cleanup: the Scheduled Task was disabled after testing; the test installation and its evidence are preserved

The `de858df` package (`e932200f…`) is a test package, not the G2 package below, and is not stored in the artifact
repository.

## G2 setup program package

The supervised-pilot customer package, packaged with `scripts/package_installer_release.py` from this release's
canonical ZIP. It is a different artifact from the G3 test package above; identify it by the hashes below.

- Package ZIP: `SortView-Setup-1.0.14.zip` (36,447,462 bytes)
- Package ZIP SHA-256: `6f91da7da2253a4cdef85e4fa6f25789dd8a48a932bc4ae884eac1465ece3193`
- Installer source: `bef28bcfcc0474d70a3510b83d13e59101202dc5` (PR #268); packaged 2026-10-25T22:51:58Z
- `SortView.Setup.exe` SHA-256: `f3aaac9c410eb9bee1f66276cc4ddd0c2820718c26c089b6476fdef9e0f8e9fc`
- Embedded bundle: the canonical release ZIP and `MANIFEST.json` above (gate Enabled, pin version `1.0.14`)
- Stored: `sortview-artifacts` `installer/1.0.14/20261025-bef28bc/SortView-Setup-1.0.14.zip`, commit `0bf29645868024556df4225c0a0367d42332558b`

Status: **unsigned** (no code signing until C1) and approved only for a **supervised pilot**. Validated by the packaging
script's own checks at build time and by the exact-package GUI installation below. **Pending:** retrieving this exact
package from the artifact repository on another machine and verifying it there against the hash above; how Windows warns
on downloading and running it has also not been validated.

## G2 validation: exact-package GUI installation

Passed on 2026-10-29 using the original G2 setup program package.

- Environment: Windows 10 Enterprise LTSC 2019 VM in VMware Workstation.
- Package: `SortView-Setup-1.0.14.zip`, SHA-256 `6f91da7da2253a4cdef85e4fa6f25789dd8a48a932bc4ae884eac1465ece3193`.
- Package integrity: verified against the archived G2 package hash before installation.
- Preflight: passed Windows prerequisites and verified all 1,392 Collector manifest entries.
- GUI installation: all seven stages completed successfully, exit code `0`.
- API: connected to the deployed SortView API over HTTPS.
- Enrollment: completed using the G2 test organization, installation ID `8`.
- Tech Logic files: empty synthetic placeholders were used for the three source paths; realistic synthetic event ingestion was not validated.
- Scheduled Task: `SortView Collector`, running as `SYSTEM`, registered and enabled with a 15-minute repetition interval.
- Manual task execution: passed on 2026-10-29 at approximately 9:57 PM Central, exit code `0`.
- Automatic task execution: passed on 2026-10-29 at 10:37:37 PM Central, exit code `0`, after refreshing the trigger.
- Cleanup: temporary diagnostic scheduled task removed; test installation ID `8` deactivated in SortView Administration.

### Scheduling qualification

The test VM initially used Pacific Time while the host used Central Time. After correcting the VM's time zone, the original scheduled trigger repeatedly advanced its next-run time without a recorded automatic execution.

A manually initiated task run completed successfully. A separate temporary scheduled task also demonstrated that Windows could execute a newly registered task automatically.

The existing Collector trigger was then edited to start at 10:37 PM Central. The Collector subsequently ran automatically at 10:37:37 PM and completed with exit code `0`.

This validates automatic execution **after trigger correction**. It does not establish that the original trigger would have worked without intervention or conclusively identify the cause of the earlier missed executions.

### Remaining validation

- Browser-downloaded unsigned-package warnings and customer-facing installation guidance remain untested.
- Retrieval of this exact package from the private artifact repository on another machine remains untested.
- Ingestion of realistic synthetic Tech Logic events through this exact G2 installation remains untested.
- Remaining release-publication checks listed below are not automatically satisfied by this installation test.

## What changed

### Fresh Contract v2 installation (`setup.ps1`)

For an enrollment code issued for a Contract v2 installation, `setup.ps1` now does all of this itself:

1. Asks the SortView API for a Contract v2 enrollment (`contract_version: 2`).
2. **Saves what a resume needs before anything else:** the non-secret enrollment details (the three IDs,
   the API address, the key id and the site's time zone) and the site's classification rules, in
   `<DataRoot>\setup` (Administrators and SYSTEM only), read back and verified. Only then is the API
   token stored (`api_token.dpapi`, DPAPI, unchanged).
3. Installs the runtime and writes a Contract v2 `collector_config.json` (`contract_mode: "v2"`, and the
   `v2` key id and time zone from the enrollment; every other v2 setting is the Collector's default).
   The config is now written atomically.
4. Installs the **server-delivered classification rules** as `<DataRoot>\config\classification_rules.json`,
   byte for byte as staged. Nothing is merged into them and no default is added.
5. Has the Collector create its **local HMAC secret** (`v2-key init`): 32 random bytes generated on the
   machine, DPAPI machine scope, bound to the server-issued key id, in the Administrators + SYSTEM only
   secrets folder. It is never sent to the server and never shown.
6. Runs the interactive and the SYSTEM-context **v2 preflight** (configuration, rules, secret, and that the
   server accepts the key).
7. **Bootstraps the v2 cursor at the line-safe end of each Tech Logic file**, so no historical record is
   uploaded.
8. Registers the Scheduled Task disabled and, because every step above succeeded, **enables it** as its
   last action. It does not start a run: the first run is the task's next 15-minute trigger.

The time zone is the one the SortView administrator recorded for the site; setup does not compare it with
the Windows time zone.

### Legacy (contract 1) codes still work

A code issued for the legacy contract is refused `contract_not_permitted` by the first request -- without
being used -- and setup then sends the legacy request once with the same code and continues exactly as
1.0.13 did, including its question before enabling the task. No other refusal or error is retried.

### Enrollment refusals are explained

The API's fixed refusals (HTTP 409) are now read and explained, and each says plainly that the code was
**not** used: a newer Collector is required, the installation is not ready to enroll, or the bundle and the
server do not agree on a contract.

When the result of the enrollment request is **unknown** -- no response, a 5xx, an unexpected status, or a
200 that could not be read -- setup no longer says the code was unused or advises getting a new one. It says
to run setup again with the same code and, if that code is then refused, to contact SortView support: the
first request may have completed, and a Contract v2 enrollment cannot be repeated with a new code.

### Resume, and what needs SortView support

A Contract v2 setup interrupted after enrollment is finished by running `setup.ps1` again: it makes no
enrollment request, reuses the stored token, the staged or installed rules and an existing valid secret
and cursor, and completes only what is unfinished. It refuses, and changes nothing, if what is on the
machine does not belong to the saved enrollment.

A Contract v2 enrollment creates the branch's one v2 identity on the server, so **a new enrollment code is
not a way to recover** -- it would be refused. These cases need SortView support:

- the enrollment succeeded on the server but its response never reached the machine;
- the API token could not be stored after enrollment (support issues a replacement token for the
  installation; setup then resumes);
- the enrolled machine is lost or replaced.

### A first install starts from a clean machine

Before the enrollment code is requested, setup now also refuses a machine that already holds an API token,
a Contract v2 secret, a cursor of either contract, or a classification rules file. None is adopted,
replaced or deleted, and replacing a stored token is no longer offered (the `-ReplaceExistingToken` switch
is removed). `tools\uninstall.ps1` keeps the data root by default, so this applies to a machine that ran a
Collector before.

### Runtime (carried from the unreleased A2 work)

- `bootstrap` seeds the v2 cursor for a `contract_mode: "v2"` config and refuses (exit 3) if a v2 or a
  legacy state file already exists.
- `preflight` adds six Contract v2 checks for a v2 config.

A scheduled run -- v1 or v2 -- is unchanged, as are both wire contracts.

### The release bundle no longer carries one library's rules

Releases 1.0.6 - 1.0.13 shipped `pilot\classification_rules.json`, generated from one library's settings,
and `tools\prepare_v2_pilot.ps1`, which could copy it over a machine's live rules. **Neither is in a 1.0.14
bundle.** A site's rules come only from the SortView API at enrollment. `tools\configure_v2.ps1` (the
in-place conversion of an existing install) is still shipped and unchanged.

### Importing the parsers no longer writes log files

Found in the Windows rehearsal of this release. The shared Tech Logic parsers configured their own log handlers when they were
imported, which created `logs\parser.acs.log`, `logs\parser.checkins.log` and `logs\parser.rejects.log` under whatever the
current directory was. During setup that was the release bundle itself: the first preflight added three files to it, and the
bundle then failed its own manifest verification, so `setup.ps1` could not be run a second time -- or resumed -- from the same
folder. On an installed machine it was the install root, where `tools\verify-install.ps1` reports unexpected files.

The parsers are now plain log producers: they attach no handler and touch no file, and `agent\logger_config.py` is no longer part
of a Collector install. Manifest verification is unchanged and still refuses any unexpected file. A legacy-contract Collector no
longer writes parser summary files under its install root (they held raw destination-label breakdowns outside the configured log
path, and grew without bound); its `collector.log` is unchanged. Contract v2 parsing was already silent and still is.

## Compatibility

- An existing install is updated with `tools\update.ps1` as before. A legacy config runs and preflights as
  v1; an existing Contract v2 install keeps its config, secret, rules and cursor.
- A 1.0.13 or older bundle cannot enroll a Contract v2 code: the API answers
  `collector_upgrade_required` and the code stays usable with this bundle.
- No server change is required by this release.

## Before this release is published

To be performed on a real Windows machine with the frozen 1.0.14 bundle, and recorded here:

- a clean install against the real API with a Contract v2 test organization -- **passed in G3** (2026-10-25);
- the SYSTEM-context preflight, with the secrets folder restricted to Administrators and SYSTEM;
- bootstrap against Tech Logic files that are being written;
- the automatic task enable, and a first scheduled run that uploads only records written after bootstrap;
- an interrupted installation, resumed;
- a rules document containing a non-ASCII account name, end to end from the real API.
