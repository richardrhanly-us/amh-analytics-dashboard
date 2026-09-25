# SortView Collector 1.0.9 — Contract v2 NBPL pilot runbook

One unambiguous sequence for preparing and dry-run-validating the Contract v2 pilot on the
production Tech Logic UltraSort machine at NBPL, using **1.0.9 release-provided tooling only**
— no operator hand-edits of `collector_config.json`, no manually-assembled JSON files, no
pieces copied over from a dev machine after the release is built.

Steps 1-3 cover **preparation and dry-run validation only** against a SEPARATE dry-run-only
config; nothing in them ever touches the production config, creates a secret, or runs live v2
ingestion. Step 4 documents the FULL live-v2 provisioning sequence that follows a PASS — each of
its lettered steps (A-J) is a separate, explicit, operator-run command; none of them chain into
the next automatically, and this runbook performs none of them for the operator.

Steps are labeled **[DEV BUILD]** (run on a development checkout) or **[SORTER]** (run on the
production Tech Logic machine) so it is never ambiguous which machine a command belongs on.

---

## 1. [DEV BUILD] Build the 1.0.9 release bundle

From a clean checkout of this repository, `collector.__version__` already at `1.0.9`
(`collector/__init__.py` — do not bump it for this pilot):

```
python -m collector.build_release --output <out-dir> --version 1.0.9 --frozen-runtime <pyinstaller-onedir-output>
```

(`collector/freeze/build_frozen.ps1` produces `<pyinstaller-onedir-output>`, typically
`dist\SortViewCollector\` — see that script's own docstring.)

This single build:

- runs `scripts/check_release_readiness.py --expect-version 1.0.9` first and refuses to build
  if the tree is not release-ready;
- **generates `pilot/classification_rules.json` at build time** from `src/branch_settings.json`
  via `collector.v2_rules.seed_from_settings` — the same logic
  `python -m collector.v2_rules seed` uses — so the artifact is never a hand-copied file
  checked into source control (`collector/build_release.py`'s `_write_pilot_rules_artifact`);
- packages `tools\prepare_v2_pilot.ps1` (the target-machine preparation tool, below) and
  `tools\configure_v2.ps1` (the production config conversion tool, step 4B below);
- the frozen runtime itself already exposes `v2-key init`/`v2-key check`
  (`collector/freeze/dispatcher.py`'s `v2-key` subcommand, forwarding to `collector/v2_keys.py`
  unchanged — step 4C/4D below), needing no separate packaging step;
- writes `MANIFEST.json` covering every file, including the above.

Zip the resulting `SortViewCollector-1.0.9\` directory into `SortViewCollector-1.0.9.zip`
(SHA-256 it for the release record — see `docs/releases/collector-1.0.4.md` for the format
a prior release used).

**Verify before shipping the ZIP anywhere:**

```
python -m pytest tests/test_v2_pilot_release_prep.py tests/test_collector_identity_collision_diag.py tests/test_configure_v2.py tests/test_collector_v2_keys.py tests/test_collector_freeze.py tests/test_v2_release_manifest.py tests/test_collector_build_release.py -q
```

## 2. [SORTER] Copy and install/update the release

Copy `SortViewCollector-1.0.9.zip` to the sorter and extract it. **Keep the Scheduled Task
disabled** (this is the normal state before/between pilot preparation runs — do not enable it
for this procedure).

If this is an update of an already-installed Collector:

```
.\tools\update.ps1 -InstallRoot "C:\SortView\Collector" -ConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json"
```

`update.ps1` disables the task before touching anything, replaces the runtime, verifies it with
preflight, and restores the task's **prior** enabled/disabled state afterward — see its own
docstring. If the task was disabled before the update (the expected pilot state), it is left
disabled. It **never** writes `contract_mode`, never touches a v2 secret/key, and never installs
or changes classification rules — this step is the ordinary 1.0.9 runtime update, nothing more.

Confirm the version and task state before proceeding:

```
& "C:\SortView\Collector\SortViewCollector.exe" version
Get-ScheduledTask -TaskName "SortView Collector" | Select-Object State
```

Expect `1.0.9` and `Disabled` (or "not found" if this is a fresh install with the task not
yet registered).

## 3. [SORTER] Prepare and dry-run-validate the v2 pilot

This is the **explicit second command** — never implied by step 2 — and the only step that
touches anything v2-related. From the extracted release bundle:

```
.\tools\prepare_v2_pilot.ps1 `
    -InstallRoot "C:\SortView\Collector" `
    -ProductionConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json"
```

Run from an elevated (Administrator) PowerShell session. It:

1. Confirms the installed Collector reports the **same version as this bundle's own
   `MANIFEST.json`** (1.0.9 for this release) — refuses otherwise.
2. Confirms the Scheduled Task `"SortView Collector"` is **not enabled** — refuses (fail
   closed) if it is armed. Not registered at all is fine.
3. Reads the **existing, unmodified** production `collector_config.json` only to derive the
   three source paths (`acs`, `checkins`, `rejects`) already configured there — never
   hardcoded — and re-verifies, at the end, that this file is still byte-for-byte identical to
   what it read.
4. Installs this bundle's packaged `pilot\classification_rules.json` to
   `C:\ProgramData\SortViewCollector\config\classification_rules.json`, validating it as JSON
   both before and after the copy.
5. Writes a **dedicated** dry-run-only config —
   `C:\ProgramData\SortViewCollector\config\collector_config.v2-dry-run.json` — containing only
   the three derived source paths, `v2.timezone: "America/Chicago"` (fixed for this NBPL
   pilot) and `v2.rules_path` pointed at the file just installed. It never writes
   `contract_mode`, `v2.key_id`, or any secret/state/status/log path.
6. Runs `SortViewCollector.exe run --config <dry-run config> --v2-dry-run` against it and
   prints the full aggregate output, then confirms every exact safety counter:
   `source_acs_present=1`, `source_checkins_present=1`, `source_rejects_present=1`,
   `throwaway_key=1`, `persistent_secret_used=0`, `network_calls=0`, `dry_run_complete=1`. Any
   missing or wrong counter is reported as **FAILED** and exits non-zero, with nothing further
   attempted.
7. Runs `SortViewCollector.exe identity-collision-diag --config <the same dry-run config>` and
   requires its printed `identity_collision_gate=pass` (`docs/collector-v2.md`'s "Onsite
   dry-run acceptance check"). This is a BOUNDED, source/category-aware rule — not a flat
   `identical_identity_events == 0` requirement — that tolerates only the two collapse patterns
   already documented and tested as intentional (an ACS hold read twice unchanged in the same
   second; a small, spread-out, low-rate cluster of barcode-less `ils_acs_failure`/
   `rfid_collision` reject duplicates) and fails closed on anything else.
   `identical_identity_events` is still always printed by that command, never hidden. A
   `identity_collision_gate=fail` (or a nonzero exit from the command itself) is reported as
   **FAILED** and exits non-zero, with nothing further attempted — the per-source detail this
   same command already printed above the gate verdict is what to investigate next.

On success:

```
V2 PILOT DRY RUN: PASS
No network calls made.
Production v1 config unchanged.
Scheduled Task remains disabled.
Ready for separate server/key/cutover preparation.
```

### What this step deliberately never does

It never sets `SORTVIEW_V2_INGEST_ENABLED`, never sets `contract_mode: "v2"`, never creates or
touches a v2 secret/key, never enables the Scheduled Task, never runs live v2 ingestion, and
never modifies the v1 state cursor. If the dry run fails (nonzero `identical_identity_events`,
or any other acceptance-gate problem), **stop and revisit the identity design** per
`docs/collector-v2.md` — do not re-run this tool hoping for a different result without
investigating first.

## 4. After a PASS: the explicit live-v2 provisioning sequence

Nothing after step 3 happens automatically — no command in this runbook chains into the next
one. Each of the following is a **separate, explicit, operator-run step**, in this order, on the
**[SORTER]** machine unless noted. Confirm the PASS from step 3 first; if it did not pass, stop
and revisit the identity design (see step 3's own "What this step deliberately never does").

**A. The server-issued `key_id` already exists.** Issued out of band
(`scripts/issue_ingest_key.py`, a separate server-side/dashboard procedure, not part of this
runbook) — a non-secret UUID4 the operator supplies to steps B and C below. For the NBPL pilot
this is `b04c3dc1-7651-4803-a593-12272dd3cfc3`.

**B. Convert the production config to v2:**

```
.\tools\configure_v2.ps1 `
    -InstallRoot "C:\SortView\Collector" `
    -ConfigPath "C:\ProgramData\SortViewCollector\config\collector_config.json" `
    -KeyId "<the key_id from step A>"
```

REQUIRES the Scheduled Task disabled (same fail-closed check as step 3). Reads the existing
production config, preserves every field it does not itself set (`customer_id`, `branch_id`,
`api_url`, `sources`, `state_path`, `status_path`, `log_path`, and anything else already
present), sets `contract_mode: "v2"` and a `v2` section (`key_id`, `timezone`, `rules_path` —
using the supported default secret/state/status/cache/quarantine paths, never inventing new
ones). Validates a CANDIDATE copy with the real collector config loader
(`support-info` + `run --v2-dry-run`) **before ever touching the production file** — a failure
here leaves production completely untouched, nothing to restore. Only once that candidate
validates does it back up the current production config (UTC-timestamped, alongside it) and
atomically replace it. Never creates a secret, never enables the task, never sets
`SORTVIEW_V2_INGEST_ENABLED`, never runs live ingestion. See
`collector/deploy/configure_v2.ps1`'s own docstring for the full sequence.

**C. Create and protect the local secret:**

```
& "C:\SortView\Collector\SortViewCollector.exe" v2-key init --config "C:\ProgramData\SortViewCollector\config\collector_config.json"
```

Frozen-runtime forwarding (`collector/freeze/dispatcher.py`'s `v2-key` subcommand) straight to
`collector.v2_keys.main` — no Python needed on the sorter, and no logic duplicated. Generates a
fresh 32-byte secret locally, stores it ONLY as a Windows DPAPI machine-scope blob, in a folder
locked to Administrators + SYSTEM (inheritance cut), binds it to the configured `key_id`, and
verifies the ACL before and after writing — a folder that cannot be verified protected means
nothing is written, and the secret is removed if verification fails after creation. Never prints
or exports the secret.

**D. Verify it:**

```
& "C:\SortView\Collector\SortViewCollector.exe" v2-key check --config "C:\ProgramData\SortViewCollector\config\collector_config.json"
```

Confirms the blob exists, DPAPI decrypts it on this machine, it is bound to the configured
`key_id`, and its ACL is still verified protected.

**E. Re-run the v2 dry-run / bounded collision validation, now against the REAL production
config** (step 3 validated a separate dry-run-only copy; this re-validates the config that will
actually run):

```
& "C:\SortView\Collector\SortViewCollector.exe" run --config "C:\ProgramData\SortViewCollector\config\collector_config.json" --v2-dry-run
& "C:\SortView\Collector\SortViewCollector.exe" identity-collision-diag --config "C:\ProgramData\SortViewCollector\config\collector_config.json"
```

Require the same exact safety counters and `identity_collision_gate=pass` as step 3. Still no
network call, still no persistent secret used (the dry run uses its own throwaway key,
independent of the one step C just created), still no live ingestion.

**F. Separately, on the server: enable `SORTVIEW_V2_INGEST_ENABLED`.** Server-side configuration,
not part of this runbook or any collector-side tool — the collector never sets this itself.

**G. Separately, on the server/dashboard: record the explicit v2 cutover timestamp**
(`scripts/set_v2_cutover.py set --customer-id ... --branch-id ... --cutover-at ...`). Also
server-side; append-only; never inferred from collector behavior.

**H. Perform ONE controlled live v2 run**, deliberately, observed, not as a background/Scheduled
Task invocation:

```
& "C:\SortView\Collector\SortViewCollector.exe" run --config "C:\ProgramData\SortViewCollector\config\collector_config.json"
```

(No `--v2-dry-run` — this is the first real network call and the first real use of the secret
created in step C, `contract_mode` now being `"v2"`.)

**I. Verify:** v2 events actually landed, no new v1-shaped writes occurred, and dashboard health
for this branch looks correct before trusting an unattended schedule.

**J. Only then, enable the recurring Scheduled Task.** Nothing before this point ever does.

---

## Reference: relevant tests

```
python -m pytest tests/test_v2_pilot_release_prep.py tests/test_collector_identity_collision_diag.py tests/test_configure_v2.py tests/test_collector_v2_keys.py tests/test_collector_freeze.py -q
```

Covers steps 1-3 (`tests/test_v2_pilot_release_prep.py`, `tests/test_collector_identity_collision_diag.py`):
the release-time generation and packaging of `pilot/classification_rules.json`
(matches `collector.v2_rules.seed_from_settings(src/branch_settings.json)`), static safety
checks of `tools\prepare_v2_pilot.ps1` (no `contract_mode`, no key/secret, no Scheduled-Task
mutation), the exact-counter safety-check logic in isolation, `Test-IdentityCollisionGatePassed`
in isolation, and full orchestration scenarios (task enabled/disabled/absent, version mismatch,
missing/invalid rules artifact, production config unchanged, generated dry-run config content,
a nonzero exit from either the dry run or `identity-collision-diag`, and an
`identity_collision_gate=fail`). The bounded gate's own thresholds (max group size, the
permitted ACS-hold-duplicate and keyless-reject-class patterns, the collision rate and
time-spread ceilings) are covered separately in `tests/test_collector_identity_collision_diag.py`
(`collector/identity_collision_diag.py`'s `_evaluate_gate`).

Covers step 4B (`tests/test_configure_v2.py`): static safety checks of `tools\configure_v2.ps1`
(no `SORTVIEW_V2_INGEST_ENABLED`, no DPAPI/key-creation call, no live ingestion), pure-function
proofs that every pre-existing config field is preserved and `Test-KeyIdFormat` matches
`collector/v2_events.py`'s `UUID4_PATTERN` exactly, and orchestration scenarios proving the
production config is left byte-for-byte untouched (no backup even created) on every failure mode
up through candidate validation, and is only replaced, atomically, after that validation passes.

Covers steps 4C/4D (`tests/test_collector_v2_keys.py`): the existing DPAPI/ACL/key-binding
behavior itself, unchanged by this release.

Covers the frozen dispatcher wiring for `v2-key` (`tests/test_collector_freeze.py`): argv
forwarding to `collector.v2_keys.main` with no logic duplicated, proven both in-process and as a
real (source-mode) subprocess.
