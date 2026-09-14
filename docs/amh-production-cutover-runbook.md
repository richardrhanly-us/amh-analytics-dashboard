# NBPL Production Cutover Runbook -- Canonical Continuous Agent

## Project state (read this first)

Real-machine shadow validation is **complete and passed** (schema v3,
binary-mode byte offsets, restart/resume, resource stability -- see
`docs/amh-live-validation-runbook.md` and the third shadow run's
preserved evidence,
`C:\ProgramData\SortView.shadow-evidence-2026-09-14-run3-pass`).

**What shadow validation did NOT prove:** the canonical agent has never
been allowed to send a real `/upload` or `/upload-pipeline-status`
request to production (`upload_enabled`/`heartbeat_enabled` were `false`
throughout every shadow run). This runbook is what closes that gap --
a small, deliberately limited, reversible first production test, not a
full unattended commercial deployment (see "What this is NOT" below).

## What this is NOT

- Not a generic installer. See `agent/README.md`'s "Remaining
  pre-production gates" and this document's own "Commercialization /
  productization work" section at the bottom for what still separates
  this from a customer-installable product.
- Not a decision to leave the canonical agent unattended for weeks
  without a human checking on it. The observation window in Stage 12
  below is deliberately active monitoring, not "set and forget."
- Not permission to skip any step below because shadow validation
  already passed. Shadow mode proved capture; it did not prove upload.

## Ground rules for the whole procedure

- `C:\SortViewAgent` (the legacy production agent) and its Scheduled
  Task are the production system of record until this runbook's
  acceptance criteria (Stage 14) are explicitly met. Nothing below edits
  files inside `C:\SortViewAgent`.
- No forensic evidence directory is ever deleted: the first failed
  shadow run's evidence, `...shadow-evidence-2026-09-14-run2`, and
  `...shadow-evidence-2026-09-14-run3-pass` all remain untouched,
  indefinitely.
- Every stage below is reversible up to the point explicitly marked
  IRREVERSIBLE-UNTIL-CONFIRMED (Stage 2 -- stopping the legacy task).
  Nothing before that point commits to anything.
- If any stage's acceptance check fails, STOP and follow the rollback
  procedure (below) rather than continuing to the next stage.

---

## Cutover architecture (what you are building)

### Install layout

| What | Location | Notes |
|---|---|---|
| Application code + venv | `C:\SortView\CanonicalAgent\` | Contains `agent\` (copied from this repo) and `.venv\Scripts\python.exe`. Read-mostly; not touched by the running agent. |
| Config | `C:\ProgramData\SortView\config\agent_runtime_config.json` | See `agent/deploy/runtime_config.production.example.json`. Contains no secret. |
| State (cursor/generation/identity) | `C:\ProgramData\SortView\state\agent_state.json` | Schema v3. |
| Spool (durable pending/quarantined batches) | `C:\ProgramData\SortView\spool\` | |
| Logs | `C:\ProgramData\SortView\logs\agent.log` | Bounded, rotating (`agent/runtime/logging_setup.py`). |
| Diagnostics | `C:\ProgramData\SortView\diagnostics\diagnostics.json` | Local-only, richer than the backend heartbeat. |
| Installation identity | `C:\ProgramData\SortView\identity\agent_identity.json` | Minted once, never regenerated except on deliberate reinstall. |
| API token | **Not a file.** A Machine-scope Windows environment variable, `SORTVIEW_API_TOKEN`. | See "Secret handling" below. |

This is the SAME `C:\ProgramData\SortView\` convention already
established and exercised by all three shadow runs -- production uses a
**separate, fresh** subtree under it (see Stage 3), never the shadow
run's own `state`/`spool`/`logs`/`diagnostics`.

### Unattended execution: Scheduled Task, not a Windows Service (for this cutover)

| | Windows Service | Scheduled Task |
|---|---|---|
| Auto-start at boot, no login | Yes | Yes (`AtStartup` trigger) |
| Restart on failure | Yes (Service Recovery) | Yes (`RestartCount`/`RestartInterval`) |
| New code/dependency required | Yes -- needs a service wrapper (pywin32 `win32serviceutil` or a third-party tool like NSSM), not present in this repo today | No -- `python -m agent.main` runs as-is |
| Matches a pattern already proven on this exact machine | No | Yes -- `C:\SortViewAgent`'s own legacy task already runs this way, successfully, for months |
| OS lifecycle integration (services.msc, `sc`) | Tighter | Looser, but sufficient (Task Scheduler UI/`schtasks`) |

**Recommendation: Scheduled Task for this cutover.** It requires no new
runtime dependency, reuses a pattern already proven reliable on this
specific machine, and is fully sufficient for the stated requirements
(auto-start, no login, restart-on-failure, clean stop, visible failure
logging). A Windows Service is the better **long-term** choice once this
becomes a packaged product (see "Commercialization work" below) and
should be revisited then, alongside a proper service wrapper -- not
invented ad hoc for a single-branch first cutover.

Use `agent/deploy/register-sortview-task.ps1` (Stage 3). It explicitly
disables Task Scheduler's default 3-day execution time limit (a common
gotcha for long-running tasks) and configures automatic restart.

**Known gap, not closed by this runbook:** the canonical runtime's own
supervisor (`agent/runtime/supervisor.py`) does not yet exit the process
when an internal component thread crashes -- it stays alive, visibly
"degraded" (see that module's own `PRODUCTION SUPERVISION POLICY`
docstring section, which already specifies the fix: log + stop siblings
+ process exit non-zero, so Task Scheduler's restart-on-failure can act
on it). Until that's implemented, Task Scheduler's restart-on-failure
will NOT fire for an internal thread death that leaves the process
itself running -- the detection net for that case, during and after this
cutover, is: the deliberately active observation window below (Stage
12), the backend's own heartbeat-staleness monitoring
(`docs/monitoring.md`), and `agent.log`/`diagnostics.json`. This is
flagged as the top follow-up item after cutover, not something this
runbook works around silently.

### Secret handling: `SORTVIEW_API_TOKEN`

The agent already only ever reads this from the environment (no code
change was needed or made). For unattended production use:

1. Provision a **separate** token for the canonical agent (do not reuse
   the legacy agent's token) via `scripts/create_agent_token.py` --
   dry-run by default, prints the token and the SQL, writes nothing
   unless `--execute` is passed. This keeps the two agents'
   `agent_tokens` rows independently revocable.
2. Set it as a Machine-scope environment variable via
   `agent/deploy/set-sortview-api-token.ps1`, run from an elevated
   PowerShell session on the AMH machine -- prompts as a SecureString,
   never echoed, never written to a file or log.
3. The Scheduled Task (running as SYSTEM by default) picks up Machine
   env vars automatically on every new process start -- no further
   wiring needed.

This satisfies: not committed to Git (never touches a file at all); not
in ordinary logs (`http_client.py` already never logs headers/payload,
independent of this); accessible to the unattended process (Machine
scope); operationally manageable by an IT admin (one script, no manual
registry editing). See "Commercialization work" below for the natural
upgrade path (Windows Credential Manager / DPAPI) once this needs to
scale beyond one admin manually running a script per site.

### Legacy-to-canonical cutover: state initialization (the core decision)

**Recommendation: stop legacy, then start canonical with a FRESH
state/spool and `bootstrap_mode: "normal"` (EOF-seed) for all three
sources, accepting and minimizing a brief (target under 2 minutes)
capture gap.**

Reasoning, addressing each option explicitly:

- **Do not reuse the shadow run's persisted state.** The shadow runs
  (including the passing run3) captured deep into the live Tech Logic
  files in CAPTURE-ONLY mode -- that state's offsets are real and
  correct, but resuming production FROM them would mean uploading a
  potentially large historical backlog spanning the entire shadow
  validation window as the very first production upload, which:
  - is unnecessary risk for a "deliberately limited and reversible"
    first test (Stage 5-6 want a small, clean, easily-verified first
    batch, not a multi-thousand-row replay);
  - would (correctly, thanks to the backend's existing semantic
    dedup -- see below) mostly no-op against rows the legacy agent
    already uploaded for that same window, which is *safe* but wastes
    the first test's signal-to-noise ratio for no benefit;
  - blurs the evidence boundary this whole engagement has carefully
    preserved between "shadow" and "production" state.
- **Do not attempt a byte-exact OFFSET-mode handoff from legacy's own
  persisted position.** This would be the theoretically zero-gap
  option, but `C:\SortViewAgent`'s own offset-persistence format
  (`data\processed\pipeline_state.json`, mentioned in the original
  onsite recon) has not been audited in this engagement -- it is a
  separate, unaudited codebase, and this project's own recent history
  (the schema-v3 incident) is a direct demonstration of how an
  offset representation can silently be wrong in a way that isn't
  obvious until it's read incorrectly. Trusting an unverified
  cross-system offset value risks either skipping real events (if it
  overstates progress) or a large, though backend-deduped-safe, replay
  (if it understates progress) -- the WRONG direction to risk is a
  silent gap, and this option can't rule that out without auditing code
  explicitly out of scope for this task (`C:\SortViewAgent` must not be
  modified, and reading it is a research side-quest this runbook
  doesn't require).
- **Why NORMAL (EOF) bootstrap, accepting a brief gap, is the right
  tradeoff:** it is the simplest, already-implemented, already-tested
  mechanism (no new code, no cross-system trust assumption). The brief
  gap between "legacy stopped" and "canonical seeds at current EOF" is
  bounded and small if the two actions happen back-to-back in one
  maintenance action (Stages 2-4, ideally during low-traffic hours) --
  a known, accepted, small operational cost, not an open-ended risk.
- **Backend dedup as a safety net, not the primary plan:** both the
  pre-existing SEMANTIC unique index
  (`customer_id, branch_id, barcode, event_time[, error_message]`,
  scoped per `c53c1b536c71`) and canonical's TRANSPORT
  `source_event_id` index (`45ba2e7befbc`) mean that even if canonical's
  very first upload window happens to overlap slightly with whatever
  legacy captured in its last run before stopping, `ON CONFLICT DO
  NOTHING` absorbs it as a no-op, not a duplicate row. This is why a
  *small* overlap is harmless and a *gap* is the risk to actively
  avoid -- which is exactly what minimizing the stop/start window (not
  maximizing it) achieves.

---

## Staged procedure

### Stage 1 -- Pre-cutover snapshot

- [ ] Run `python scripts/verify_db_snapshot.py` against production
      `DATABASE_URL` and save the output (row counts, latest event per
      table, current `alembic_version`) -- this is your "before" picture
      to compare against after cutover and, if needed, during rollback
      investigation.
- [ ] Confirm zero `acs_events` rows with NULL `customer_id` or
      `branch_id` in production (`SELECT count(*) FROM acs_events WHERE
      customer_id IS NULL OR branch_id IS NULL;`) -- an explicit
      pre-deployment gate carried over from migration `c53c1b536c71`'s
      own documented caveat, never confirmed in this engagement. If any
      exist, resolve before proceeding -- they sit outside the
      tenant-scoped unique index's protection.
- [ ] Confirm legacy's last several Scheduled Task runs succeeded
      (Task Scheduler history + `run_pipeline.log` under
      `C:\SortViewAgent`) -- do not cut over on top of an already-failing
      legacy run.
- [ ] Confirm the dashboard shows current data and normal `pipeline_status`
      freshness for this branch.
- [ ] Confirm `C:\SortView\CanonicalAgent\` has the intended code
      checked out/copied, with its own `.venv` built and
      `python -m agent.main --help`-style sanity check working.
- [ ] Provision the canonical agent's production token
      (`scripts/create_agent_token.py`, dry run first, then `--execute`)
      and set it via `set-sortview-api-token.ps1`. Do this BEFORE Stage
      2 so there is no token-provisioning delay once legacy is stopped.
- [ ] Fill in `agent/deploy/runtime_config.production.example.json`
      (real `customer_id`/`branch_id`, confirm `api_url`) and save it to
      `C:\ProgramData\SortView\config\agent_runtime_config.json`. Leave
      `upload_enabled`/`heartbeat_enabled` UNSET (production mode) --
      this is the real cutover, not another shadow run.
- [ ] Register the Scheduled Task (`register-sortview-task.ps1`) but do
      **not** start it yet.

### Stage 2 -- Legacy stop (IRREVERSIBLE-UNTIL-CONFIRMED: production ingestion pauses here)

- [ ] Disable (not delete) `C:\SortViewAgent`'s Scheduled Task, or wait
      for its current run to finish and disable the next trigger --
      whichever your maintenance window prefers. Confirm no legacy run
      is mid-flight before proceeding.
- [ ] Note the exact wall-clock time legacy stopped. This is the
      reference point for how long the capture gap (see architecture
      section above) actually was.

### Stage 3 -- Canonical starting-state initialization

- [ ] Confirm `C:\ProgramData\SortView\state\`, `spool\`, `logs\`,
      `diagnostics\`, `identity\` do not already contain shadow-run
      content -- if this machine's shadow runs used the SAME
      `C:\ProgramData\SortView\` root, their state/spool/logs/
      diagnostics must already be relocated to their own preserved
      evidence folders (already true per the current onsite state: run2
      and run3-pass are already preserved separately) and these active
      directories must be empty/fresh for production.
- [ ] Confirm `agent_runtime_config.json`'s three `sources` entries all
      use `"bootstrap_mode": "normal"` (the default if omitted, but
      confirm explicitly by reading the file, not assuming).

### Stage 4 -- Canonical production start

- [ ] `Start-ScheduledTask -TaskName "SortView Canonical Agent"`.
- [ ] Confirm the exact wall-clock start time. The gap between Stage 2's
      stop time and this start time is your actual capture gap --
      record it.
- [ ] Tail `agent.log` and confirm the startup banner shows `MODE:
      NORMAL PRODUCTION MODE (uploads and heartbeat both enabled)` --
      if it shows anything else, STOP and fix the config before
      proceeding further.
- [ ] Confirm all three sources bootstrap at their current EOF (no
      historical flood) via `diagnostics.json`'s per-source `offset`
      roughly matching each file's real current size.

### Stage 5 -- Authentication check

- [ ] Confirm the FIRST `/upload-pipeline-status` heartbeat call
      succeeds (200, `agent.log` shows "Heartbeat sent"). This proves
      the token authenticates and is scoped to the right
      `customer_id`/`branch_id` before any event data is at stake.
- [ ] If it fails with 401/403: STOP. Check the token was actually set
      (new process, Machine scope, correct value) and that
      `agent_tokens.is_active` is true and scoped correctly. Do not
      proceed to event uploads with an unresolved auth failure.

### Stage 6 -- First upload acknowledgement

- [ ] Wait for the first real Tech Logic check-in (or reject/ACS event)
      to occur naturally -- do not synthesize test data into production
      source files.
- [ ] Confirm `agent.log` shows "Batch delivered" for it, and the
      corresponding spool batch file is gone from `pending/` (acked).
- [ ] Confirm via `scripts/verify_db_snapshot.py` (or a direct query)
      that a new row appears in the relevant table
      (`checkins`/`rejects`/`acs_events`) with a non-null
      `source_event_id` -- this is the canonical-specific proof, since
      legacy rows always have `source_event_id IS NULL`.

### Stage 7 -- Heartbeat confirmation

- [ ] Confirm `pipeline_status.updated_at` for this branch continues
      advancing at the configured `heartbeat_interval_seconds` cadence
      (default 60s) over at least a 10-minute window.
- [ ] Confirm `health_status` reads `"healthy"` (not `degraded`/
      `auth_failure`) absent any deliberately-induced condition.

### Stage 8 -- Spool drain / trend check

- [ ] Over at least 30-60 minutes of real traffic, confirm
      `diagnostics.json`'s `pending_batch_count`/`pending_bytes` per
      source stay low and trend toward zero between events (draining),
      not accumulating.
- [ ] Confirm `quarantined_batch_count` stays at 0 (a quarantine this
      early is a signal to pause and investigate the specific
      record/reason, not proceed).

### Stage 9 -- Backend/database/dashboard confirmation

- [ ] Confirm the Streamlit dashboard reflects the new canonical-sourced
      rows normally (live view, counts, no tracebacks).
- [ ] Spot-check a handful of canonical-sourced rows against what Tech
      Logic actually recorded for the same events (parity check, same
      spirit as the shadow run's Stage 2 parser parity, now against
      real production writes).

### Stage 10 -- Generation/offset check

- [ ] Confirm `checkins`/`rejects`/`acs` all remain at `generation 0`
      (no false rotation/truncation) through the observation window.
- [ ] Confirm offsets are advancing monotonically and look like sane
      byte counts (schema-v3 sanity check, same class of verification
      the shadow run already did).

### Stage 11 -- Resource check

- [ ] Confirm working set / private bytes stay in the same range shadow
      validation already measured (~91 MB / ~168 MB) -- a significant
      departure under real upload load is worth understanding before
      calling this stable.
- [ ] Confirm CPU usage stays modest and log file rotation is engaging
      normally (bounded, not unbounded growth).

### Stage 12 -- Observation window

- [ ] Keep both the canonical agent and active human monitoring running
      for a deliberately limited window (recommend: one full business
      day, during hours a person can respond) before considering this
      more than a smoke test. Do not walk away and call it done after
      Stage 6-11 pass once.
- [ ] During this window, the LEGACY agent stays disabled (not
      re-enabled "just in case") -- this is the actual test: can
      canonical alone carry production for NBPL.

### Stage 13 -- Rollback trigger criteria

Any ONE of the following during Stages 4-12 triggers rollback (below),
not "wait and see":

- Stage 5's authentication check fails and is not resolved within the
  maintenance window.
- Any quarantined batch appears (Stage 8) whose cause isn't immediately
  understood and clearly not indicative of a broader problem.
- `health_status` reports `auth_failure` at any point after Stage 5
  passed once (a credential that stops working mid-run is more
  concerning than one that never worked).
- Generation on any source increments unexpectedly (Stage 10) -- treat
  exactly as seriously as the second shadow run's ACS failure did.
- Spool backlog grows without draining for longer than
  `disk_pressure_pending_bytes_threshold` would tolerate, or disk
  pressure pause engages unexpectedly.
- Resource usage grows unboundedly (a leak) rather than staying stable.
- Dashboard/backend data (Stage 9) doesn't match what Tech Logic
  actually recorded.
- Any unhandled exception/crash in `agent.log` during the observation
  window that isn't immediately, confidently explained.

### Stage 14 -- Final acceptance criteria

All of the following, not any subset:

- [ ] Stages 5-11 all passed with no rollback-trigger condition
      observed during Stage 12's full observation window.
- [ ] No data loss or duplication found on inspection (Stage 9).
- [ ] The team is willing to leave legacy disabled going into the next
      business day without an active human watching continuously.
- [ ] The fail-fast supervision gap (see architecture section above) is
      either closed, or explicitly accepted as a known residual risk
      with the heartbeat-staleness monitoring net understood by whoever
      is on call.

Only once Stage 14 is fully met should `C:\SortViewAgent`'s Scheduled
Task be considered for actual removal/decommission -- that is a
SEPARATE, later, explicit decision this runbook does not make on its
own, consistent with `docs/release-process.md`'s existing "AMH agent
deployment" boundary language.

---

## Rollback procedure

Can be executed at any point in Stages 4-13, and is designed to be
low-drama:

1. **Stop the canonical agent cleanly.**
   `agent/deploy/unregister-sortview-task.ps1` (without `-RemoveTask`,
   if you intend to retry soon; with `-RemoveTask` for a fuller
   rollback). This only stops/removes the Scheduled Task -- it does not
   touch `C:\ProgramData\SortView\state\spool\logs\diagnostics`.
2. **Preserve its state/spool/logs as-is.** Do nothing further to them
   -- do not delete, do not "clean up." If you plan to retry the
   cutover later, rename this attempt's directories aside (matching the
   same evidence-preservation pattern already used for the shadow runs)
   before Stage 3 re-initializes fresh ones for the retry.
3. **Re-enable legacy.** Re-enable/start `C:\SortViewAgent`'s Scheduled
   Task (the trigger you disabled in Stage 2).
4. **Confirm legacy uploads resume.** Wait for its next run, confirm
   `pipeline_status.updated_at` for this branch is advancing again from
   legacy (no `source_event_id` on the new rows), and that legacy is not
   erroring on a stale/incorrect persisted offset (it should simply
   resume from wherever it left off, per the original onsite recon's
   confirmed offset-persistence behavior).
5. **Investigate without service disruption.** Production ingestion is
   back on legacy at this point -- there is no urgency pressure on
   diagnosing the canonical failure. Use `agent.log`,
   `diagnostics.json`, and the preserved state/spool from the failed
   attempt. Do not delete any of it.
6. Do not re-attempt cutover until the specific rollback-trigger cause
   is understood and, if it implies a code gap, fixed and covered by a
   regression test -- the same standard this project has held itself to
   throughout the shadow-validation phases.

---

## Commercialization / productization work (explicitly NOT part of this cutover)

Kept separate on purpose -- none of this blocks or is required for the
NBPL cutover above:

- A real installer (MSI/EXE) that lays out `C:\SortView\CanonicalAgent\`
  and its `.venv` automatically, instead of a manual copy.
- Proper Windows Service packaging (a pywin32 service wrapper or
  equivalent) plus the fail-fast supervisor change noted above --
  Service Recovery is the better long-term restart mechanism once this
  exists.
- An onboarding/config wizard (currently: hand-edit the example JSON).
- Automated API token provisioning as part of a tenant-onboarding flow,
  and a stronger secret store than a Machine env var (Windows Credential
  Manager / DPAPI) once more than one admin needs to manage tokens
  across more than one site.
- An update/upgrade mechanism (currently: manual code copy + task
  restart; the schema-v3 boundary this project just went through shows
  why update tooling needs to know how to detect and message a required
  state reset, not silently resume).
- A diagnostics bundle command (zip up `diagnostics.json` + recent
  `agent.log` + config with the token redacted, for support requests).
- A clean uninstall path (stop task, optionally purge state/spool with
  explicit confirmation -- today's tooling deliberately never auto-
  deletes anything, which is correct for THIS phase but would need an
  explicit, deliberate "yes I mean it" uninstall flow for a real
  product).
- Multi-tenant/multi-branch setup UX (today: one `runtime_config.json`
  per install, hand-edited per branch).
