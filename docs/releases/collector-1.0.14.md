# SortView Collector 1.0.14 Release Notes

## Status

- Release: `1.0.14`
- `collector.__version__`: `1.0.14`
- Canonical release artifact: **not built yet**. The fingerprint, the frozen-runtime record and the
  installation validation are added to this document when the release is built and rehearsed (see
  *Before this release is published*). Nothing here has been validated on a Windows machine yet.

1.0.14 is the first Collector release whose guided setup can install a **new** machine directly on
Contract v2. **Contract 2 is not yet what a newly provisioned installation gets:** Super Admin still
creates a contract 1 installation unless an administrator selects Contract v2 explicitly. Making it the
default is a later, separate change, made only after this bundle has been rehearsed and published.

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

## Compatibility

- An existing install is updated with `tools\update.ps1` as before. A legacy config runs and preflights as
  v1; an existing Contract v2 install keeps its config, secret, rules and cursor.
- A 1.0.13 or older bundle cannot enroll a Contract v2 code: the API answers
  `collector_upgrade_required` and the code stays usable with this bundle.
- No server change is required by this release.

## Before this release is published

To be performed on a real Windows machine with the frozen 1.0.14 bundle, and recorded here:

- a clean install against the real API with a Contract v2 test organization;
- the SYSTEM-context preflight, with the secrets folder restricted to Administrators and SYSTEM;
- bootstrap against Tech Logic files that are being written;
- the automatic task enable, and a first scheduled run that uploads only records written after bootstrap;
- an interrupted installation, resumed;
- a rules document containing a non-ASCII account name, end to end from the real API.
