# SortView AMH Agent

This folder contains three distinct things. Knowing which is which matters more than anything else here:

| What | Where | Status |
|---|---|---|
| **Actual deployed production agent** | `SortViewAgent - What is currently sitting on the AMH computer/` | Running on the real AMH machine today, via Task Scheduler, unchanged by any work in this repo. Frozen historical snapshot -- see its own section below. |
| **New canonical continuous agent** | `main.py`, `runtime/`, `parser/`, `state.py`, `spool.py`, `event_identity.py`, `identity.py`, `discovery.py`, `tailer.py` | Built and extensively tested (unit + real-thread integration) in this repo. **Not yet run on the real AMH machine.** This is what will eventually replace the archived baseline, after live validation. |
| **Legacy/validation-only local mirror** | `run_pipeline.py`, `uploader.py`, `config.py`, `logger_config.py`, `parse_checkins.py`, `parse_rejects.py`, `parse_acs.py` | A pre-continuous-ingestion, scheduled-style local port of the deployed baseline's logic. Never deployed to the AMH machine. Kept only as a runnable comparison point for later side-by-side validation. Not canonical. |

AMH-machine inspection is complete: the real Tech Logic log paths, the deployed agent's actual runtime/config, and the existing Scheduled Task have all been examined directly. **Live validation of the new canonical agent on that machine remains pending** -- see "Remaining pre-production gates" below. Treat those as two separate milestones, not one.

## What is the production agent, right now?

`SortViewAgent - What is currently sitting on the AMH computer/`. It runs from `C:\SortViewAgent` on the AMH machine via a 15-minute Task Scheduler entry, invoking `python run_pipeline.py` from that folder. Nothing in this repository's cleanup work has touched it, and nothing will until an explicit, separate cutover decision is made (see "Remaining pre-production gates"). Do not edit it -- it is preserved as read-only historical evidence of five months of real production behavior; see its own README inside that folder for details.

## The new canonical agent

Entry point: **`python -m agent.main --config <path-to-runtime-config.json>`**, run from the repository root (or an install root with `agent/` as a subfolder, same convention as before).

```
agent/main.py
  -> agent.runtime.supervisor.AgentRunner
       -> collector    (tailer -> parser -> event_identity -> spool -> state, per configured source)
       -> uploader      (spool -> HTTP -> ack/retry/quarantine)
       -> heartbeat      (state/spool snapshot -> POST /upload-pipeline-status)
       -> housekeeping   (disk-pressure check, stale temp cleanup, local diagnostics)
```

### Config

A JSON file you point `--config` at (see `agent/runtime/config.py:load_runtime_config` for the exact schema) -- customer/branch identity, API URL, per-source paths and bootstrap modes, batching/retry/heartbeat/disk-pressure thresholds. The API token is **never** in the config file -- it comes from the `SORTVIEW_API_TOKEN` environment variable (same variable name the legacy path also uses, read differently). Every numeric/behavioral setting can be overridden by an environment variable named `SORTVIEW_RUNTIME_<KEY_UPPERCASE>` without editing the file.

No path is hardcoded into the code. The intended production layout (a convention for whoever eventually packages this, not something enforced by any test):

```
C:\ProgramData\SortView\
  config\agent_runtime_config.json
  state\agent_state.json
  spool\
  logs\
  diagnostics\
```

### Mutable state, spool, logs

- **State** (`state_path` in config): one atomically-written JSON file (`agent/state.py`) tracking, per source, the durable read cursor, a logical generation number (bumped on rotation/truncation), and the last-known file identity. Survives restarts; never advances past data that isn't already durably spooled.
- **Spool** (`spool_root`): NDJSON batch files under `pending/<source>/` and `quarantine/<source>/` (`agent/spool.py`). This is the durability boundary -- an event is safe the moment it lands here, before any HTTP call is ever attempted.
- **Logs** (`log_dir`): one bounded, rotating log file (`agent/runtime/logging_setup.py`), never the unbounded plain file the legacy path uses.
- **Diagnostics** (`diagnostics_dir`): a local-only JSON snapshot (source generations/offsets/paths, component health, disk-pressure state) written by housekeeping -- richer detail than what's sent to the backend heartbeat, kept local rather than added to the backend schema.

### Bootstrap modes

Set per source in the config. Applies only the very first time a source has no persisted state:

- **NORMAL** (safe default): seeds at the source file's current end-of-file. Historical content already in the file is never read.
- **REPLAY**: starts at byte 0, explicitly. Reads and spools everything already present. Must be requested; never implied.
- **OFFSET**: starts at an explicit caller-supplied byte offset. A validation/testing tool, not a normal production mode.

### What happens during a backend outage

Nothing is lost. `RETRYABLE_INFRA` (connection errors, timeouts, 429, 5xx) and `AUTH_FAILURE` (401/403) both leave the affected spool batch pending, untouched, with exponential backoff between retries -- never quarantined for either reason, regardless of how long the outage lasts. Collection keeps running and keeps spooling independently of whether uploads are succeeding. A `PERMANENT_REJECTION` (400/413-shaped) is isolated down to the smallest practical unit via bounded, budgeted splitting -- one bad record never blocks its healthy siblings, and only the record(s) that independently reproduce the rejection are quarantined, with evidence preserved.

### What happens after a restart

The collector reloads persisted state and resumes from the exact durable cursor; any spool batches already on disk are picked up by the uploader exactly as before. If the process died with events sitting in memory but not yet flushed to the spool, nothing is lost or duplicated in the sense that matters: a fresh read simply reproduces the same bytes and the same deterministic `source_event_id`, which the backend's transport-idempotency index recognizes as the same event.

### Shadow / capture-only validation mode

Two config keys, both defaulting to `true` (production-safe by default -- a config that never mentions them behaves exactly like normal production):

- `upload_enabled` (default `true`): when `false`, the uploader thread keeps running on its normal schedule but never calls `/upload` -- collection, `source_event_id` generation, state, and spool all continue completely unaffected.
- `heartbeat_enabled` (default `true`): when `false`, the heartbeat thread keeps computing its health snapshot on its normal interval (logged locally) but never POSTs it to `/upload-pipeline-status`.

This is **not** implemented via a broken URL or an invalid token -- both flags are checked before any HTTP client call is even constructed, so disabling them never generates retry/backoff/error noise the way a genuine outage would.

Setting both to `false` is **SHADOW / CAPTURE-ONLY VALIDATION MODE** -- the intended mode for the first live-AMH-machine validation run (see `docs/amh-live-validation-runbook.md`). The active mode is impossible to miss: it's printed as a loud banner in the startup log, and recorded explicitly (`mode`, `upload_enabled`, `heartbeat_enabled`) in every `diagnostics.json` snapshot.

Because shadow mode never drains the spool, pending batches accumulate for as long as the run continues -- disk-pressure safeguards (pending data is never deleted) stay fully active regardless, and `diagnostics.json` reports `pending_batch_count`/`pending_bytes` per source on every housekeeping cycle so backlog can be watched during the run. See the runbook for how to size a safe shadow-run duration from real throughput/disk numbers rather than a guessed constant.

## What old architecture was removed

An experimental SQLite-backed continuous-ingestion path (`agent/outbox.py`, `agent/outbox_uploader.py`, `agent/watcher.py`, `agent/maintenance.py`, and a SQLite-aware `agent/heartbeat.py`) was built earlier in this engagement, proved out the real reliability requirements (crash-safe restart, retry/backoff, poison-event isolation, disk-aware housekeeping, health reporting), and has since been deleted now that the canonical runtime above covers every one of those requirements with file-based durability instead of a local database. See git history for the removed files; every requirement they encoded has an equivalent canonical test (state/spool/uploader/heartbeat/housekeeping test suites) -- SQLite was not a design goal to preserve, and the canonical runtime has none.

## Remaining pre-production gates

Not done yet, in no particular order beyond roughly "closest to done first":

- **Live validation of the new canonical agent on the already-inspected AMH machine** -- continuous read behavior while Tech Logic actively writes, real file-locking behavior, real rotation/truncation behavior, actual CPU/memory/disk footprint, reboot/recovery behavior, and side-by-side parity against the legacy production pipeline over sustained real-world operation. Nothing above can be proven from this repo's tests alone. A staged runbook for this specific visit is ready: `docs/amh-live-validation-runbook.md` (shadow mode, Stages 0-7, stops before any real upload is enabled).
- Production database schema verification/migration (the Phase E migrations have been run and proven against a disposable Postgres instance, never against production).
- An audit of any pre-existing NULL `customer_id`/`branch_id` rows in `acs_events` before the tenant-scoped unique index change reaches production (see the Phase E report's known caveat).
- Windows Service fail-fast supervision behavior (recorded as a design decision in `agent/runtime/supervisor.py`; not yet implemented).
- Windows Service packaging and an installer.
- The side-by-side legacy/new-agent validation run itself.
- Production cutover.

None of the above have been started as part of this cleanup phase.
