# AMH Live-Validation Runbook -- Canonical Continuous Agent

## Project state (read this first)

**AMH machine inspection is already complete.** The environment, the
existing deployed runtime, the Scheduled Task, the real Tech Logic log
paths, and the deployed baseline have all already been examined directly
on the real machine. Nothing in this runbook is "go inspect the
machine" -- that milestone is done.

**What remains is specifically live validation of the new canonical
continuous agent (`agent/main.py` + `agent/runtime/*`) on that
already-inspected machine.** This runbook is for that visit/session.

Do not collapse "inspection" and "live validation" into one milestone in
any report written during or after this runbook -- they are separate,
and only the second one is what this document covers.

## Ground rules for the whole session

- The legacy Scheduled Task stays enabled and authoritative throughout
  every stage below. It is never disabled, paused, or modified.
- Tech Logic's own files are never edited, truncated, rotated, or
  deleted by this process. No destructive testing of vendor files.
- The new canonical agent runs in **SHADOW / CAPTURE-ONLY VALIDATION
  MODE** (`upload_enabled: false`, `heartbeat_enabled: false`) for the
  entirety of Stages 0-6. No production database writes, no production
  heartbeat writes, from the new agent, until an explicit later approval
  enables Stage 7's follow-on (parallel production uploading) -- which
  this runbook does not perform.
- The new agent's mutable state (config/state/spool/logs/diagnostics)
  lives under its own separate directory tree (e.g.
  `C:\ProgramData\SortView\`), never inside or alongside the archived
  baseline's install root (`C:\SortViewAgent`).

---

## Schema v3 transition (one-time, before Stage 0)

Do this once, the first time a v3-or-later canonical agent build runs
against this AMH install after a pre-v3 (schema v1/v2) shadow run on it --
skip entirely if this install has never run a pre-v3 build. See
`agent/README.md`'s "Schema v3 compatibility boundary" section for why
this is required: v3 changed what `cursor.offset` means (a true physical
byte offset, not an opaque text-mode cookie -- see `agent/tailer.py`'s
OFFSET REPRESENTATION docstring section for the incident that forced
this), so pre-v3 state and spool content cannot be resumed from or merged
into v3 runtime state.

- [ ] Confirm every prior shadow run's evidence (state, spool, logs,
      diagnostics) is preserved under its own untouched, clearly-labeled
      folder, separate from the active runtime directories -- e.g.
      `C:\ProgramData\SortView.shadow-evidence-2026-09-14-run2` for the
      most recent run. If a prior run's evidence has already been moved
      out of the active `C:\ProgramData\SortView\` directories, this box
      is already checked -- do not move it again. If it has NOT yet been
      moved out, move it aside now (rename/relocate, never delete)
      before proceeding.
- [ ] Never delete any preserved evidence folder, at any point, for any
      reason -- this applies to every prior run's folder, not just the
      most recent one.
- [ ] Confirm `C:\SortViewAgent` (the legacy production agent and its
      Scheduled Task) has not been touched and is not part of this
      transition in any way.
- [ ] Confirm the active `C:\ProgramData\SortView\` `state`, `spool`,
      `logs`, and `diagnostics` directories contain no carried-over
      content from any pre-v3 run -- create them fresh and empty for
      this v3 shadow run if they don't already exist in that state.
- [ ] Do NOT copy, merge, or otherwise reuse any v1/v2 `state.json` or
      spool batch into the new v3 directories -- v3 refuses to load a
      v1/v2 `state.json` outright (`UnsupportedSchemaVersionError`), and
      old spool content's `source_event_id` values are not compatible
      with v3's offset representation regardless of that check.
- [ ] Verify the runtime config for this run has `upload_enabled: false`
      and `heartbeat_enabled: false` set explicitly, read from the file
      itself -- the same requirement as Stage 0 below, called out again
      here since this transition is a natural point to double-check it
      before the agent ever starts.

**Only proceed to Stage 0 once every box above is checked.** This
transition step does not itself grant approval for production cutover --
that remains a separate, later, explicit decision as described in
Stage 7.

---

## STAGE 0 -- Safety check

Before starting the new agent at all, confirm:

- [ ] The legacy Scheduled Task is enabled and its last run succeeded
      recently (check Task Scheduler history and `run_pipeline.log`).
- [ ] The real Tech Logic paths match what's configured
      (`Checkins.txt`, `Rejects.txt`, `ACS Log.txt` -- confirm against
      the archived baseline's `agent_config.json` for the exact paths
      already known from inspection).
- [ ] The new canonical agent's runtime config points `state_path`,
      `spool_root`, `log_dir`, and `diagnostics_dir` at a location
      **entirely separate** from the legacy agent's install root and
      data files -- verify by reading the config file directly, not by
      assumption.
- [ ] The runtime config has `upload_enabled: false` and
      `heartbeat_enabled: false` set explicitly (do not rely on
      defaults being correct by luck -- read the file).
- [ ] `SORTVIEW_API_TOKEN` is either unset or set to a value that is
      never actually used this session (shadow mode never sends it
      anywhere, but confirm no other process on the machine would pick
      it up unexpectedly).
- [ ] Confirm (by code, not assumption) that shadow mode cannot produce
      a production write -- see this repo's `agent/README.md` "Safety
      properties" section for the structural reason (`upload_enabled`/
      `heartbeat_enabled` are checked before any HTTP client call is
      even constructed for that request; they are not implemented via a
      broken URL or bad token).

**Do not proceed to Stage 1 until every box above is checked.**

---

## STAGE 1 -- Short live capture

- [ ] Start the canonical agent manually: `python -m agent.main --config <path>`.
- [ ] Confirm the startup log banner shows `MODE: SHADOW / CAPTURE-ONLY
      VALIDATION MODE` -- if it doesn't, stop and fix the config before
      continuing.
- [ ] Confirm active log discovery: the log should show each configured
      source being found (or, for a source not yet written to this
      cycle, correctly reported as not-yet-existing rather than erroring).
- [ ] Confirm EOF bootstrap behavior: for a source seeing its first-ever
      contact with this agent, state should seed at the file's *current*
      end-of-file, and no historical content should flood into the
      spool. Confirm by checking `diagnostics.json`'s per-source
      `offset` immediately after startup roughly matches the real file's
      current size, and that `pending_batch_count` stays at 0 until
      genuinely new lines are written after that point.
- [ ] With Tech Logic actively writing in the background (normal
      operation, not forced), observe that new events appear in the
      spool (`pending\<source>\*.ndjson` under the configured
      `spool_root`) shortly after Tech Logic writes them.
- [ ] Confirm the persisted cursor (`state.json`'s per-source `offset`)
      only advances *after* a spool batch has actually landed on disk --
      never ahead of it. Compare timestamps/order between a new spool
      file appearing and the state file's `offset` changing.
- [ ] Confirm Tech Logic itself is never interrupted, delayed, or shows
      any lock contention symptoms while the new agent tails its files
      alongside the legacy agent.

---

## STAGE 2 -- Parser/output parity

For the same real source records, compare what the new canonical parser
produced (visible in spooled NDJSON records) against what the legacy
path produced (its own processed CSVs / what it uploaded):

- [ ] Checkins: barcode, title, destination (raw and normalized),
      collection code, call number, shelf code, bin, flags, is_problem,
      message, timestamp.
- [ ] Rejects: barcode, error message (raw and simplified), timestamp.
- [ ] ACS: message code, barcode, title, patron ID, destination, raw
      message, timestamp.
- [ ] Destination normalization specifically -- confirm the same raw
      destination strings map to the same normalized labels
      (Main/Westside/Library Express/No Agency Destination) in both paths.
- [ ] Timestamp parsing -- confirm identical datetimes for the same
      source lines, including any bad-datetime-coerced-to-NaT cases.
- [ ] Malformed/short/skipped records -- confirm the new agent skips
      exactly the same lines the legacy parser would skip, for the same
      reason.

**Document any discrepancy found -- do not assume the new path is
correct merely because it's newer.** A discrepancy here is a real
finding to report, not something to silently patch over during the
visit.

---

## STAGE 3 -- Continuous-read behavior

Over several real Tech Logic write cycles (not synthetic ones):

- [ ] Observe partial-line behavior if Tech Logic happens to write a
      line incrementally (buffered/flushed in pieces) -- confirm the
      agent never consumes an incomplete line, and correctly picks up
      the complete line once finished.
- [ ] Confirm no file-locking conflicts between the legacy agent, the
      new agent, and Tech Logic's own writer, all reading/writing the
      same files concurrently.
- [ ] Record the actual `SourceIdentity` values observed (opaque, but
      confirm they stay stable across ordinary appends and only change
      on genuine rotation).
- [ ] Confirm `generation` in `state.json` stays constant during normal
      append activity (it must only increment on rotation/truncation --
      see Stage 6).
- [ ] Confirm no duplicate spool records appear during ordinary,
      uneventful polling -- cross-check `source_event_id` values across
      consecutive spool batches for the same source; every ID should be
      unique.

---

## STAGE 4 -- Resource measurement

Measure on the real AMH machine, using built-in Windows tools only (Task
Manager, `Get-Process`, Resource Monitor, `Get-PSDrive` / `Get-Volume`
for disk) -- do not add a permanent production dependency (e.g. psutil)
solely for this measurement; a temporary, uninstalled-afterward tool is
acceptable if a built-in one genuinely isn't sufficient.

- [ ] Idle CPU% (agent running, no new Tech Logic activity for a stretch).
- [ ] Active CPU% (during a real Tech Logic write burst).
- [ ] Memory / working set (idle and active).
- [ ] Spool growth rate (bytes/hour, extrapolated from the observed
      capture window -- see "Shadow-run duration" below for why this
      number specifically matters).
- [ ] Log file growth rate (confirm rotation actually engages once
      `agent.log` approaches its configured `max_bytes`).
- [ ] Free disk space on the volume `spool_root`/`log_dir` live on,
      before and after the observation window.

**Do not fabricate any of these numbers if they cannot actually be
measured during the visit -- report "not measured" explicitly rather
than estimating.**

---

## STAGE 5 -- Restart test

- [ ] Stop the NEW canonical agent only (Ctrl+C or equivalent) -- the
      legacy agent and its Scheduled Task are untouched throughout.
- [ ] Confirm clean shutdown in the log (no unhandled exceptions, "agent
      stopped cleanly" or equivalent).
- [ ] Confirm any spool batches that existed before stopping still exist
      on disk afterward, byte-for-byte.
- [ ] Restart the canonical agent with the same config.
- [ ] Confirm state reload: the log/diagnostics should show the same
      per-source generation/offset the agent had before stopping, not a
      fresh bootstrap.
- [ ] Confirm NO historical reread occurred -- no flood of old events
      into the spool on restart.
- [ ] Confirm the pending spool batches from before the restart are
      still there, untouched, and (since still in shadow mode) still not
      uploaded.
- [ ] Append a small amount of genuinely new Tech Logic content (if any
      occurred naturally during the restart window) and confirm
      collection resumes correctly from exactly where it left off.

---

## STAGE 6 -- Optional controlled source-behavior observations

**Do NOT force destructive Tech Logic rotation/truncation merely for
testing.** This stage is passive observation only.

- [ ] If Tech Logic naturally rotates or truncates a source file during
      the validation window (this may or may not happen -- do not wait
      indefinitely for it), capture: the log lines showing rotation/
      truncation detected, the `generation` increment in `state.json`,
      and confirm the pre-rotation pending batch (if any) and
      post-rotation batch both still exist independently in the spool,
      correctly ordered.
- [ ] If no natural rotation/truncation occurs during the window, leave
      this item explicitly **unproven** in the Stage 7 report rather
      than manipulating vendor files to force it.

---

## STAGE 7 -- Shadow-run review

**STOP before enabling any backend upload.** Before any config change
toward parallel production uploading, produce a report covering:

- Events captured (counts per source, over the observation window).
- Parser parity result (Stage 2's findings -- match or documented
  discrepancies).
- Duplicate findings (Stage 3's `source_event_id` uniqueness check).
- Errors encountered, if any (with log excerpts).
- File-lock findings (Stage 1/3).
- Source identity behavior (Stage 3).
- Resource measurements (Stage 4 -- or "not measured" where applicable).
- Spool growth rate and total accumulated size by end of the window.
- Restart outcome (Stage 5).
- Rotation/truncation outcome: observed-and-verified, or explicitly
  left unproven (Stage 6).
- Remaining unknowns -- anything this runbook didn't cover, or couldn't
  resolve during this visit.

**A later, separate, explicit approval is required before enabling
`upload_enabled`/`heartbeat_enabled` for parallel production uploading.**
This runbook does not grant that approval by completing Stage 7 --
completing Stage 7 only produces the report that such a decision would
be based on.

---

## Shadow-run duration

This runbook deliberately does **not** prescribe a universal duration
("run it for N hours") -- how long a first shadow run should reasonably
be allowed to run depends on two numbers this repo cannot know without
the real machine: actual Tech Logic write throughput (events/hour) and
actual available disk space at `spool_root`'s location. Shadow mode
intentionally never drains the spool (see "Shadow-mode spool growth"
below), so the safe duration is bounded by:

    safe_duration_hours ≈ available_free_bytes / observed_bytes_per_hour

Compute this using Stage 4's real spool-growth-rate measurement once a
short initial window (Stage 1-3, perhaps 30-60 minutes) has produced a
real throughput number -- do not guess a throughput figure in advance.
The disk-pressure safeguard (`disk_pressure_min_free_bytes`,
`disk_pressure_pending_bytes_threshold`) will pause collection before
the disk is actually exhausted regardless, but a shadow run is more
useful the less it relies on that safeguard actually firing.

## Shadow-mode spool growth

Because capture-only mode intentionally never drains pending batches
(there is no uploader activity to acknowledge them):

- Disk-pressure safeguards remain fully active in shadow mode -- see
  `agent/runtime/housekeeping.py`. Pending data is never deleted to
  satisfy a size limit, in shadow mode or otherwise.
- `diagnostics.json` (`<diagnostics_dir>/diagnostics.json`) reports
  `pending_batch_count` and `pending_bytes` per source on every
  housekeeping cycle -- check this periodically during the run rather
  than only at the end.
- If disk pressure engages during a shadow run, that is expected
  behavior once the configured thresholds are reached, not a bug --
  confirm the log shows collection pausing, not erroring.
