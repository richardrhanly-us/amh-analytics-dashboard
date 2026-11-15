# C6a R4 — VM qualification of machine replacement

**Status (2026-11-14): backend qualification PASSED; Windows end-to-end qualification DEFERRED.**
- **Stage 0 (baseline):** passed on 2026-11-13, with the follow-ups listed at the end of its record. It verified the starting state only.
- **Reduced backend qualification:** passed on 2026-11-14 (record below). It rests on automated tests and on a live staging environment, which were set up but did not run a replacement.
- **Stage 1 (Windows rehearsal on staging), steps 1b–1d:** deferred. VMware cloning was blocked and there was not enough disk space for a full VM copy. **No replacement has been run on a Windows computer.**
- **The full R4 acceptance below is therefore not met.** The criteria still open are listed under *Acceptance criteria still open*.
- **Staging cleaned up on 2026-11-14:** the DigitalOcean app is deleted, and the Neon project `sortview-r4-staging` is deleted and awaiting permanent removal after Neon's seven-day retention. Production was checked afterwards (checklist below).
- **Production stages:** not planned. They need a product-owner decision (end of this document).
- The production replacement flag stays off.

**For:** the person running the qualification, a platform administrator with VMware Workstation on the host and read-only access to the production database.

**What R4 must show** — the original, full acceptance (`docs/c6a-replacement-machine-design.md`, 14.9), unchanged: replacement works on real Windows computers with the unchanged G2 package.
- A first replacement of an active, ingesting source, installation `9`, after which:
  - the source's next scheduled run is refused and inserts nothing;
  - the replacement's first automatic run returns `0` and it becomes Active;
  - appended synthetic records are ingested;
  - the source and its key are retired, the replacement owns the one active key, the source's rows are unchanged, and the cutover is unchanged.
- A second replacement, to a third computer.
- An interrupted setup that resumes after redemption, and a lost response that is recovered.

## Rules for every stage

- Use only synthetic data and the test libraries named here. Never use a customer library or customer data.
- Never record an enrollment code, agent token, password, connection string or Collector secret in notes, tickets, screenshots or chat. Key ids are recorded as their first 8 characters.
- Production database access is read-only. Every production query is in `scripts/c6a_r4/` and runs inside a rolled-back read-only transaction.
- VM-A, which runs installation `9`, is never reverted, cloned from its current state, or changed by a test.
- If any check fails, stop and record what you saw. Don't fix anything in order to continue.

## Stage 0 — baseline (flag off, nothing changes)

Stage 0 checks that installation `9` can serve as the source, and records the "before" evidence that a production replacement would be compared against. It changes nothing on the server.

### Checklist

**0.1 The package.** Run `Get-FileHash .\SortView-Setup-1.0.14.zip -Algorithm SHA256`. **Pass:** `6F91DA7DA2253A4CDEF85E4FA6F25789DD8A48A932BC4AE884EAC1465ECE3193`.

**0.2 SortView Administration (production).** Open **Libraries → sortview-g2-qualification-2 → Installations**. **Pass:**
- The notice "Machine replacement is turned off on this server. A pending replacement can still be cancelled." is shown.
- No card has **Replace this Collector computer**.
- Installation #9 is Active, Contract 2, at the primary branch, and was seen within 30 minutes.
- No card has a *Replacement* fact. Note the library's Collector time zone.

**0.3 VM-A.** In an elevated PowerShell window, check:
- the time zone (`Get-TimeZone`);
- the `SortView Collector` task (`Get-ScheduledTask`, `Get-ScheduledTaskInfo`);
- the end of `C:\ProgramData\SortViewCollector\logs\collector.log`;
- the files in `C:\TLCFinalDlls`.

**Pass:** the time zone matches the library's; the task is `Ready` with `LastTaskResult` `0` and recent runs; the latest run is healthy; the three files exist. Don't start the task by hand.

**0.4 The clean base snapshot.** **Pass:** Windows 10 Enterprise LTSC 2019, no SortView installation, and available to clone without changing VM-A.

**0.5 Production database baseline (read-only).**

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h <host> -U <read-only role> -d <database> -W `
    -v slug=sortview-g2-qualification-2 -v installation=9 `
    -f scripts\c6a_r4\stage0_baseline.sql | Tee-Object r4-stage0-db.txt
```

**Pass:**
- one primary branch;
- one active key, owned by installation `9`;
- no active unbound token;
- one cutover;
- no replacement and no replacement audit events;
- every section 9 column is `t`.

### Stage 0 record (2026-11-13): passed

**0.1 The package**

| Item | Result |
|---|---|
| Package | `SortView-Setup-1.0.14.zip`, at `C:\Users\SortView\Downloads\` on the VM |
| SHA-256 | `6F91DA7DA2253A4CDEF85E4FA6F25789DD8A48A932BC4AE884EAC1465ECE3193` |
| Against the release record | **Matches** the full 64-character package ZIP SHA-256 in `docs/releases/collector-1.0.14.md` (G2 setup program package), compared character by character, ignoring case |

- That release record is what `scripts/package_installer_release.py` takes its expected hashes from.
- The comparison used the hash computed on the VM. Its file was not hashed again independently.
- The bundle's own `MANIFEST.json` hash (`8758e0f6…`) is a different check. Setup verifies it, and all 1,392 manifest entries, in its bundle-verification step. That step runs on the first installation that uses this copy, so it is **not yet checked** for this copy of the package.

**0.2 Production administration**
- The R3 interface is deployed at `admin.sortviewapp.com`.
- The installation-list API returns `replacement_enabled=false` and `pending_replacement_id=null`, and `replaces_installation_id=null` for installation `9`.
- On library `sortview-g2-qualification-2`, installation #9 is Active, Contract 2, at the primary branch, and *Last seen* advances normally.
- The replacement-disabled notice is shown, and there is no Replace button.
- **The production replacement flag is off.**

**0.3 VM-A (installation 9)**
- Windows 10 LTSC 2019, time zone Central Standard Time.
- Scheduled task `SortView Collector`: Ready, last result `0`.
- Executable `C:\SortView\Collector\SortViewCollector.exe`; configuration `C:\ProgramData\SortViewCollector\config\collector_config.json`.
- Logs report `health=healthy`, `schedule_status=healthy` and `last_error_class=None`.
- The 2026-11-12 synthetic uploads inserted 13 events. Later scheduled runs with no new records completed successfully.

**0.4 Clean base snapshot**
- Snapshot: `M2c3-Clean-Win10-LTSC2019-Net472`, created 2026-10-22.
- Windows 10 Enterprise LTSC 2019, build 17763, x64, .NET Framework 4.7.2.
- Its description says no SortView installation and no test files.
- Available as the source of a clone. VM-A was not reverted or changed.

**0.5 Production database** (`scripts/c6a_r4/stage0_baseline.sql`, read-only transaction, ended with `ROLLBACK`)
- **Library:** organization #9 `sortview-g2-qualification-2`; primary branch #8 `g2-qualification`.
- **Installations:** installation #9 is Active, Contract 2. There are no replacements.
- **Key:** one active ingest key, record #8, owned by installation #9.
- **Tokens:** one active agent token, record #21, owned by installation #9; no active unbound token.
- **Cutover:** one Contract v2 cutover, record #6.
- **Codes:** one used enrollment code, and no live unused code.
- **Checks:** all seven section 9 checks returned `t`:
  - `source_active_contract2_here`
  - `source_seen_in_30_min`
  - `one_active_key_owned_by_source`
  - `no_active_unbound_tokens`
  - `source_has_active_token`
  - `one_cutover`
  - `no_replacement_yet`

**Historical events: 13**, all under installation `9`'s key:

| Table | State | Count | `rows_fingerprint` |
|---|---|---:|---|
| `acs_item_events` | hold | 5 | `6652505ab0b12d8895af7d7eb39a64f0` |
| `acs_item_events` | non_hold_101 | 1 | `b9384cbc1510096ada8e35b75e91650f` |
| `acs_item_events` | other_code10 | 1 | `0add02c172f13f017c44505ab8973723` |
| `checkin_events` | — | 4 | `2d429312df81b9402bf17f70dc2d0c56` |
| `reject_events` | — | 2 | `cba09cd24f32bc933c1ebb8a2978cc0d` |

The 4 check-ins and 2 rejects include the two records from the malformed first fixture of 2026-11-12, as explained in the 1.0.14 release notes.

**Invariants a production replacement must keep.** These are recorded here and **not yet tested**:
- the five fingerprints and counts above stay exactly the same;
- cutover record #6 stays exactly the same;
- key #8 and token #21 are retired or deactivated, never deleted.

New events after a replacement appear under the replacement's key only.

**Follow-ups from stage 0** (none blocks stage 1):
- **Library Collector time zone:** not recorded. VM-A uses Central Standard Time, and the 2026-11-12 counts were as expected. Confirm the library's setting reads `America/Chicago`.
- **Bundle manifest check:** pending, as in 0.1.
- **The clean snapshot** predates the time-zone fix of 2026-10-29, and has no Tech Logic files. Both are fixed on each clone, never on the snapshot (stage 1b).
- **Network for test VMs:** not yet configured or verified (stage 1b).

## Reduced backend qualification record (2026-11-14): passed

This is a **reduced** qualification of the server side only. It is recorded separately from, and does not replace, the full R4 acceptance at the top of this document.

### Automated test evidence

Run on the developer host. These tests exercise the code; they say nothing about real Windows computers.

| Suite | Command | Result |
|---|---|---|
| Database-independent replacement tests | `python -m pytest tests/test_admin_replacement.py tests/test_replacement_flag.py tests/test_replacement_redemption.py -q --tb=short` | **109 passed** in 5.26 s |
| PostgreSQL integration tests | `python -m pytest tests/test_admin_replacement_postgres.py tests/test_replacement_redemption_postgres.py -q --tb=short` | **90 passed** in 38.33 s |
| **Combined** | | **199 passed, 0 failed** |

The PostgreSQL tests ran against a local PostgreSQL server, with `SORTVIEW_TEST_POSTGRES_URL` pointing to `localhost`, on disposable test databases. They did not touch the Neon staging project or production.

### Live staging observations

Observed by hand on the staging environment built from `docs/c6a-r4-staging-setup.md` (stage 1a).

| Item | Observed |
|---|---|
| Neon project | Separate project `sortview-r4-staging`, database `sortview_r4_staging` |
| Migrations | Completed at Alembic head `b8e2d4f61a07` |
| Runtime role `sortview_app` | Restricted privileges verified: 31 relations, 23 sequences, no `DELETE` or `TRUNCATE` |
| Database identity | Staging identity marker and endpoint separation from production verified |
| Administrator | One staging platform administrator created |
| Backend | Separate DigitalOcean app deployed; `GET /` returned HTTP `200` |
| Admin app | The local React admin app signed in against staging |
| Library | `sortview-r4-staging` provisioned |
| Installation | Contract 2 installation #1, `R4-VM-A-Original`, created; it **remains Provisioning** (never enrolled by a Collector) |
| Replacement flag, staging | **Disabled** throughout this work |
| Replacement flag, production | Disabled: per the stage 0 baseline of 2026-11-13, and the disabled notice was still shown after cleanup on 2026-11-14 |

Because the staging flag stayed off and no Collector enrolled, **no replacement was authorized, redeemed or cancelled on staging**. Stage 1a's "flag on" pass criterion was not exercised.

### Windows qualification: deferred, not passed

- VMware clones from snapshot `M2c3-Clean-Win10-LTSC2019-Net472` were attempted. Cloning was blocked, and the host lacked the storage for a full VM copy.
- No replacement was performed on a Windows computer. VM-A and installation `9` were not touched.
- Therefore R4 has **not** shown any of the following:
  - the unchanged G2 installer (GUI or console engine) completing a replacement;
  - scheduled Collector operation on a replacement computer;
  - credential revocation on a real retired computer (its next scheduled run refused);
  - the end-to-end replacement workflow, including the interrupted-resume and lost-response paths.

### Acceptance criteria still open

Each needs real Windows computers, so none can be closed by the reduced qualification:

| Original R4 criterion | Status |
|---|---|
| First replacement of an active, ingesting source | Open |
| Source's next scheduled run refused, inserting nothing | Open |
| Replacement's first automatic run returns `0` and it becomes Active | Open |
| Appended synthetic records ingested under the replacement's key | Open |
| Source and key retired, one active key owned by the replacement, source rows and cutover unchanged (stage 0 invariants) | Open (covered by automated tests only, not on a real source) |
| Second replacement to a third computer | Open |
| Interrupted setup resumed after redemption | Open |
| Lost response recovered | Open |
| Flag-off refusal of an issued code, and cancellation (stage 1 step 1d.5) | Open on a real computer |
| Runbook steps work as written (1d.6) | Open |
| Bundle manifest check on this copy of the package (stage 0 follow-up) | Open |

Closing them needs either the stage 1 rehearsal with working VM clones (more disk space, or clones on another host), or the production-session decision below.

## Staging cleanup verification checklist

Cleanup was done on 2026-11-14. Items marked *not reported* were not part of the cleanup report; they don't block R4, but are left open so they aren't mistaken for checked. The detailed commands are in `docs/c6a-r4-staging-setup.md`, section 8.

**Staging removal**
- [ ] Any evidence still needed from staging is saved first. *Not reported.*
- [x] DigitalOcean: staging app `lobster-app` (`c6bf181c-f935-4476-bc3a-f1cc3f95f8bc`) deleted.
- [x] `doctl apps list` shows only the production app `sortview-app`.
- [ ] `https://<staging app>.ondigitalocean.app/` no longer answers. *Not reported* (the app's deletion is confirmed above).
- [x] Neon: the **project** `sortview-r4-staging` deleted, which removes its database, roles, administrator and data. It is listed under Neon's *Recently deleted* projects until permanent deletion after the seven-day retention period (about 2026-11-21).
- [ ] After the retention period, the project no longer appears under *Recently deleted*. *Pending.*
- [x] Host: `frontend\.env.development.local` removed; the temporary staging database environment variables cleared.
- [ ] Temporary SQL and hash files deleted, and the staging administrator's password removed from any password manager entry made for it. *Not reported.*
- [ ] Any partial VM clones or copies deleted from disk. *Not reported.* VM-A and snapshot `M2c3-Clean-Win10-LTSC2019-Net472` are not to be touched.

**Production safety**
- [x] `doctl apps list` still shows the production app `sortview-app`.
- [ ] The Neon production project is still present and unmodified. *Not reported.*
- [x] Production admin: installation #9, `G2 Windows 10 LTSC Qualification`, is Active, Contract 2.
- [x] The machine-replacement-disabled notice is shown, and no pending replacement is shown.
- [ ] The production installation-list API returns `replacement_enabled=false`. *Not re-checked through the API;* the admin notice above reflects the same flag.
- [x] VM-A's scheduled Collector ran automatically at 2026-11-14 6:43:43 PM with `LastTaskResult=0`; the next run was scheduled for 6:58:58 PM.
- [x] Production admin shows installation #9 *Last seen* 2026-11-14 6:43 PM CST, so the Collector reached production after VM-A's restart.
- [ ] Optionally, `stage0_baseline.sql` still returns the stage 0 counts and fingerprints. *Not run.*

**Record:** staging app deleted 2026-11-14; Neon project deleted 2026-11-14 (permanent deletion pending after seven-day retention); production checks done 2026-11-14.

## Stage 1 — isolated staging rehearsal (1a done; 1b–1d deferred)

**Why.** Every replacement step changes credentials and cannot be undone. Stage 1 rehearses the whole R4 sequence on a backend that holds no customer data, with the replacement flag on there and only there. Production keeps the flag off throughout. Stage 1 also tests the tools and the VM procedure, so any production stage only repeats a sequence that is already known to work.

**What it cannot prove.** The unchanged G2 package's GUI talks only to the production address, so stage 1 runs Setup's own console engine with `-ApiUrl`. The bundle, the Collector and the enrollment protocol are exactly the same. Only the GUI front end, and installation `9`, are not exercised.

### 1a. Staging backend and database

**Status (2026-11-14):** built, and recorded under *Reduced backend qualification* above. The flag was never turned on in staging, so the "no flag-off notice" pass item below was not exercised. Stages 1b–1d are deferred: VM cloning was blocked by disk space.

The exact procedure is in `docs/c6a-r4-staging-setup.md`. In short:
- **Database:** a new, empty Neon project (never a branch of production), migrated to head, with the runtime role `sortview_app` given exactly production's privileges.
- **First administrator:** a guarded, one-off insert on the staging database only.
- **Backend:** a throwaway DigitalOcean app from `main` at `46ac69a`, with new secrets only and the flag turned on only after the isolation checks pass.
- **Admin app:** the R3 admin app run locally on `http://localhost:5174`, with its development proxy set by `DEV_API_PROXY_CHANGE_ORIGIN=true` to reach the DigitalOcean host (that document, section 1, item 1, resolved).
- **Seed data, through the admin app:** library `sortview-r4-staging` with one active primary branch, its operational identity, and Collector settings (time zone `America/Chicago`, synthetic classification lists).

**Pass:**
- `GET /` answers `200`.
- The Installations page for `sortview-r4-staging` loads, shows **no** flag-off notice, and allows **Add installation**.
- The production admin app and production API are untouched. Their notice still shows, and `replacement_enabled=false` is unchanged.

### 1b. Windows VMs (VM-A untouched)

**Clones.** Create **full** clones, not linked clones, from snapshot `M2c3-Clean-Win10-LTSC2019-Net472`:
- `R4-S`, the staging source;
- `R4-B`, the first replacement;
- `R4-C`, the second replacement.

More can be made from the same snapshot for the lost-response steps.

- VMware Workstation can clone from an existing snapshot while the parent VM keeps running. If it refuses because VM-A is running, **stop**: don't power off or suspend VM-A without a planned window, because installation `9` would miss scheduled runs.
- A full clone doesn't depend on VM-A's snapshot chain later.

**On each clone, before anything else:**
- Rename the computer (`R4-S`, `R4-B` and so on), so enrollment hostnames can't be confused with VM-A's, then restart.
- Set the time zone to Central Standard Time and confirm Windows time sync.
- Create `C:\TLCFinalDlls` with three empty files: `ACS Log.txt`, `Checkins.txt` and `Rejects.txt`.
- Copy the package; check its SHA-256 as in 0.1; extract it.
- Copy the two PowerShell tools to `C:\SortView-R4\`.
- Take a snapshot named `R4-<name>-ready`.

**Network.** Use VMware NAT, as used for G3, on every clone. Check from each clone in an elevated PowerShell window:

```powershell
Resolve-DnsName <staging app>.ondigitalocean.app
Test-NetConnection <staging app>.ondigitalocean.app -Port 443
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
(Invoke-WebRequest https://<staging app>.ondigitalocean.app/ -UseBasicParsing).StatusCode
Get-NetAdapter | Select-Object Name, Status
```

**Pass:**
- the name resolves; port 443 is reachable; `/` returns `200` with no certificate warning;
- exactly one adapter is `Up`, and its name is recorded for the watcher.

### 1c. Tool dry runs (staging clones only, never VM-A)

**Batch writer.** On the host, make batch `S0`. Append it on `R4-S` **before** any Setup, while no Collector is reading.

**Pass:**
- the script reports the three appends;
- the files are their old bytes followed by the batch;
- the ACS file has 13 lines and 26 separators;
- running it again with `S0` is refused, and nothing is written.

Revert `R4-S` to `R4-S-ready` afterwards.

**Watcher.** On `R4-C` only:
- start `Watch-SetupRedemption.ps1`;
- create `C:\ProgramData\SortViewCollector\setup\enrollment-recovery.json` by hand, as an empty file;
- run `-Restore`.

**Pass:**
- within a second, the watcher reports the record and disables the adapter;
- `Test-NetConnection` then fails;
- `-Restore` brings the adapter back, and `/` answers again.

Revert `R4-C` to `R4-C-ready` afterwards. It must have no setup folder when Setup runs for real.

### 1d. Replacement rehearsal (staging)

Run Setup on a clone in an elevated console, using the package's own engine:

```powershell
cd <extracted package>\bundle
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -ApiUrl https://<staging app>.ondigitalocean.app
```

- Setup asks for the enrollment code itself, hidden.
- **On resume, Setup reuses the address saved with the enrollment.** Either omit `-ApiUrl` or pass exactly the same one. A different address is refused, by design: the token belongs to the server that issued it.
- Without `-ApiUrl`, Setup would contact production. A staging code would be refused there without being used, but don't rely on that.
- Confirm the exact path of `setup.ps1` inside `bundle\` on the first clone.
- **Run the stage 0 SQL against staging before and after each step**, with `-v slug=sortview-r4-staging` and the current source's installation id. Before step 1, nothing has been enrolled, so the source-id checks don't apply yet.

**Steps**, in the order of the R4 acceptance:

1. **Source.** Enroll `R4-S` on staging as an ordinary Contract 2 installation. Then:
   - let one automatic run complete;
   - append batch `S1`;
   - let the next run ingest it;
   - record the stage 0 SQL output.
2. **First replacement, S → B.** Authorize, then issue the code.
   - Take a snapshot of `R4-B` immediately before Setup.
   - Run Setup on `R4-B`.
   - Append batch `S2` on `R4-S`, then wait for `R4-S`'s next scheduled run.
   - Wait for `R4-B`'s first automatic run, then append batch `B1` on `R4-B`.
3. **Second replacement, B → C, interrupted after redemption.** Start the watcher on `R4-C`, then run Setup.
   - When Setup stops, record what it says.
   - Run `-Restore`, then run Setup again with the same `-ApiUrl` and choose resume.
4. **Lost response, C → D.** On a fresh clone `R4-D`, run the watcher and Setup, then revert `R4-D` to its ready snapshot.
   - Authorize E with D as the source, ticking the lost-response confirmation.
   - On a fresh clone `R4-E`, disconnect the network at the code prompt, submit the code, and record the unknown result.
   - Reconnect, then run Setup again on the same computer with the same code.
5. **Flag off.** Authorize F and issue its code. Then set `SORTVIEW_MACHINE_REPLACEMENT_ENABLED=false` **on staging**, which redeploys it.
   - Enter F's code on a fresh clone.
   - Cancel F with **Cancel replacement**.
6. **Clean up** the retired computers as runbook sections 6 and 7 describe.

### Stage 1 evidence and pass criteria

| Step | Pass | Evidence |
|---|---|---|
| 1a Backend | Health answers; staging admin works with the flag on; production unchanged with the flag off | screenshots; the app spec with no values |
| 1b Clones | Five full clones; VM-A still running and uploading; the network checks pass | clone list; VM-A's last run result; the command output |
| 1c Tools | Exact appends and refusals; the adapter cut within a second and restored | the script output |
| 1d.1 Source | Active; `S1` ingested as 5 hold, 1 non_hold_101, 1 other_code10, 3 check-ins, 1 reject | stage 0 SQL output |
| 1d.2 First replacement | Setup exit `0`; `collector_replacement_redeemed` recorded. **S:** key and installation retired, tokens inactive; its next run refused and `S2` not ingested; its rows and fingerprints unchanged. **B:** one active key, owned by B; first automatic run `0` and Active; `B1` ingested under B's key. **Cutover:** unchanged | stage 0 SQL output before and after; `collector.log` from S and B |
| 1d.3 Interrupted resume | Setup stops after redemption; resume completes with no new code; the same checks as 1d.2 for B → C | Setup output; SQL |
| 1d.4 Lost response | D is used, owns the key, Provisioning; E is authorized with the confirmation recorded in the audit; Setup on `R4-E` succeeds after an unknown result (the 14.6 check) | Setup output; SQL; audit rows |
| 1d.5 Flag off | Setup reports "not ready to be enrolled" and the code stays unused; cancellation works; `admin_replacement_cancelled` recorded | Setup output; SQL |
| 1d.6 Runbook | Each runbook step works as written, or the difference is recorded | notes |

Stage 1 passes only when every row passes. Then throw the staging app and database away, and record that they were deleted.

As of 2026-11-14 only row 1a has evidence, and only partly: the staging admin worked, but with the flag off. Rows 1b–1d.6 are deferred.

## Decisions for the product owner

1. **Production qualification.** R4's acceptance names installation `9` and the unchanged GUI, and both need the flag on in production. Options:
   - approve a time-boxed production session after stage 1 passes, with the flag on, used only on `sortview-g2-qualification-2`, then turned off and checked off the same day;
   - or accept stage 1, staging with the console engine, as R4 evidence, and change the acceptance text in 14.9.
2. **Staging:** approved 2026-11-13, as a new, empty Neon database and a temporary DigitalOcean backend, and provisioned (stage 1a). The admin app proxy change was made. The production-session decision is still deferred.
3. **Windows qualification (new, 2026-11-14):** stage 1 steps 1b–1d could not run because VM cloning was blocked by disk space. Options:
   - provide the storage (or another VMware host), rebuild staging, and resume stage 1 from 1b;
   - or record R4 as backend-only and carry the open criteria above to a later release.

   The staging resources were deleted on 2026-11-14, so resuming stage 1 means rebuilding staging from `docs/c6a-r4-staging-setup.md`.
