# deployment

This document explains how to deploy and validate the main parts of SortView.

## system parts

SortView currently has three operational parts:

- backend API
- Streamlit dashboard
- AMH agent

These parts are related, but they are deployed differently.

## backend API deployment

The backend API is responsible for:

- receiving `/upload`
- receiving `/upload-pipeline-status`
- authenticating the AMH agent
- writing data into Neon

Typical backend deployment flow:

1. make code changes locally
2. commit and push to GitHub
3. deploy the backend service
4. if schema changes are included, run Alembic migrations
5. verify API health

## backend validation checklist

After backend deployment, verify:

- the API starts successfully
- `/` responds normally
- agent uploads do not return 500 errors
- `pipeline_status` rows continue updating
- recent checkins/rejects/ACS rows continue appearing in Neon

## dashboard deployment

The Streamlit dashboard is responsible for:

- loading checkins, rejects, ACS data, and pipeline status
- rendering live, overview, report, and transit views
- providing super admin / settings / user management UI

Typical dashboard deployment flow:

1. make code changes locally
2. commit and push to GitHub
3. deploy or refresh the Streamlit app
4. verify dashboard pages load successfully
5. verify tenant schema validation passes
6. verify live and historical data appear correctly

## dashboard validation checklist

After dashboard deployment, verify:

- app loads without a traceback
- login works
- main branch dashboard loads
- latest pipeline status appears
- latest checkin in DB is not unexpectedly missing
- live today metrics behave correctly
- reports and overview pages load
- super admin pages load if applicable

## AMH agent deployment

The AMH agent is the Windows-side process that runs near the sorter and uploads data to the API.

The repo stores the source-of-truth code in `agent/`, but the live deployed copy runs on the AMH-attached Windows machine.

The agent runs as a Python package (`python -m agent.run_pipeline`), not a
standalone script -- every module under `agent/` uses package-relative
imports (`from .config import ...`), so it only works invoked that way.

Typical AMH deployment flow:

1. update and validate source in `agent/`
2. copy the approved `agent/` folder to the AMH machine, keeping it as a
   subfolder of whatever install root you're deploying to (e.g.
   `C:\SortViewAgent\agent\`) -- not flattened into that root directly,
   since the package import needs `agent/` to still be a package
   directory, not a pile of loose top-level scripts
3. confirm local config (`agent/agent_config.json`) is still correct
4. from the install root (`C:\SortViewAgent\`, the parent of `agent/` --
   never from inside the `agent/` folder itself), run:

       python -m agent.run_pipeline

5. review logs for:
   - successful start status upload
   - successful batch upload
   - successful completed status upload
   - state file update
6. re-enable normal scheduled execution

## Local outbox maintenance (Continuous Ingestion Phase 5: Sustain)

`agent/maintenance.py` is a separate, independent component from the
uploader -- its own interval, its own connection, no HTTP calls. It keeps
the local SQLite outbox (`agent/outbox.py`) safe for long-term unattended
operation:

- **Retention**: delivered rows (`uploaded_at` set) older than
  `SORTVIEW_OUTBOX_DELIVERED_RETENTION_DAYS` (default 7) become eligible
  for deletion. Age is measured from `uploaded_at` (actual delivery time),
  not `created_at` (when the watcher first captured it).
- **Quarantined rows are never auto-pruned**, regardless of age. There is
  no operator-facing tool to review or clear them yet (out of scope for
  Phase 5) -- they simply accumulate in `local_events` until a human looks
  at them directly in the database.
- **Pending rows are never auto-pruned**, regardless of age -- deleting an
  undelivered row would be data loss.
- **Batched, bounded deletes**: `SORTVIEW_OUTBOX_PRUNE_BATCH_SIZE` (default
  1000) rows per transaction, oldest-first, committed between batches;
  `SORTVIEW_OUTBOX_MAINTENANCE_MAX_BATCHES_PER_CYCLE` (default 10) caps how
  many batches one cycle may run, so a very large backlog drains gradually
  across cycles instead of monopolizing SQLite in one long-running cycle.
- **Cadence**: `SORTVIEW_OUTBOX_MAINTENANCE_INTERVAL_SECONDS` (default
  3600 -- once an hour), deliberately independent of the uploader's 2-5s
  poll cadence.
- **WAL checkpointing**: `PRAGMA wal_checkpoint(TRUNCATE)` once per cycle,
  after pruning -- the only checkpoint mode that actually shrinks the
  `-wal` file's on-disk size (PASSIVE/RESTART checkpoint the WAL's content
  into the main file but leave the file's current size unchanged). A
  checkpoint under contention (another connection still needs some WAL
  frames) blocks the calling thread for up to that connection's own
  busy-wait timeout before giving up -- not an instant return. The
  maintenance component uses a short, dedicated timeout for this reason
  (`SORTVIEW_OUTBOX_MAINTENANCE_BUSY_TIMEOUT_SECONDS`, default 5s, vs. the
  other components' 30s), so a contended checkpoint blocks for at most a
  few seconds, then simply retries on the next hourly cycle.
- **Space reclamation is limited on existing databases.** A brand-new
  agent database (created after this deployment) is created with
  `auto_vacuum=INCREMENTAL`, so `PRAGMA incremental_vacuum` can actually
  shrink its file over time as rows are pruned
  (`SORTVIEW_OUTBOX_MAX_INCREMENTAL_VACUUM_PAGES` bounds how much one
  cycle reclaims). An **existing** database (anything that has already run
  through Phases 1-4) was created with `auto_vacuum=NONE`, and there is no
  way to change that retroactively without a full `VACUUM` (a full file
  rebuild) -- which Phase 5 deliberately does not perform, on a running
  agent or otherwise. On an existing database, pruned rows' pages are
  still freed onto SQLite's internal freelist and reused by future
  inserts (so the file doesn't grow unbounded from normal churn), but the
  `.db` file itself will not shrink back down to reflect that reuse -- it
  stays at its current high-water mark. A one-time rebuild/compaction path
  for existing databases is intentionally deferred to later work.

## AMH validation checklist

After agent deployment, verify:

- parser runs complete without exceptions
- offsets update correctly
- upload returns HTTP 200
- completed status returns HTTP 200
- new rows appear in Neon
- dashboard reflects new data

## deployment order when multiple parts change

If multiple parts change at once, use this order:

1. database migration
2. backend API
3. dashboard
4. AMH agent

This reduces the chance of payload/schema mismatches.

## important rules

- do not run database migrations from the AMH machine
- do not manage schema in request handlers
- do not rely on startup-time schema creation in production
- future schema changes should use Alembic
- agent changes must be manually deployed to the AMH machine

## environment variables

Every environment variable the codebase reads is listed in
[`.env.example`](../.env.example) at the repo root, with a comment on each
one. That file is a documentation template only -- nothing in this repo
loads a `.env` file automatically (no `python-dotenv`), so treat it as the
source of truth to copy from into whatever actually sets env vars for each
target: a real `.env` plus your own process manager for local dev,
Streamlit Cloud's Secrets panel for the dashboard, the AMH machine's
scheduled task environment for the agent, CI secrets for the pipeline.

| Variable | Target | Required? | Default |
|---|---|---|---|
| `DATABASE_URL` | backend, dashboard | required | -- (dashboard falls back to `st.secrets["DATABASE_URL"]`) |
| `SENTRY_DSN` | backend | optional | unset = Sentry disabled |
| `SENTRY_ENVIRONMENT` | backend | optional | `development` |
| `SORTVIEW_UPLOAD_RATE_LIMIT` | backend | optional | `30/minute` |
| `SORTVIEW_MAX_REQUEST_BODY_BYTES` | backend | optional | `5242880` (5 MB) |
| `SORTVIEW_ALLOWED_ORIGINS` | backend | optional | `http://localhost:8501,http://127.0.0.1:8501` |
| `SORTVIEW_DEMO_MODE_ENABLED` | dashboard | optional | `false` |
| `SORTVIEW_GUEST_EMAIL` | dashboard | required if demo mode on | -- |
| `SORTVIEW_GUEST_PASSWORD` | dashboard | required if demo mode on | -- |
| `SORTVIEW_APP_URL` | dashboard | required for password reset | -- |
| `SORTVIEW_SMTP_HOST` | dashboard | required for password reset | -- |
| `SORTVIEW_SMTP_PORT` | dashboard | optional | `587` |
| `SORTVIEW_SMTP_USERNAME` | dashboard | required for password reset | -- |
| `SORTVIEW_SMTP_PASSWORD` | dashboard | required for password reset | -- |
| `SORTVIEW_EMAIL_FROM` | dashboard | required for password reset | -- |
| `SORTVIEW_ALLOW_FILE_FALLBACK` | dashboard | optional | `false` |
| `SORTVIEW_CHECKINS_ORG_COLUMN` | dashboard | optional | `customer_id` |
| `SORTVIEW_CHECKINS_BRANCH_COLUMN` | dashboard | optional | `branch_id` |
| `SORTVIEW_REJECTS_ORG_COLUMN` | dashboard | optional | `customer_id` |
| `SORTVIEW_REJECTS_BRANCH_COLUMN` | dashboard | optional | `branch_id` |
| `SORTVIEW_ACS_ORG_COLUMN` | dashboard | optional | `customer_id` |
| `SORTVIEW_ACS_BRANCH_COLUMN` | dashboard | optional | `branch_id` |
| `SORTVIEW_PIPELINE_ORG_COLUMN` | dashboard | optional | `customer_id` |
| `SORTVIEW_PIPELINE_BRANCH_COLUMN` | dashboard | optional | `branch_id` |
| `SORTVIEW_LIVE_TIMEZONE` | dashboard | optional | `America/Chicago` |
| `SORTVIEW_API_TOKEN` | agent | required | -- |
| `SORTVIEW_HTTP_CONNECT_TIMEOUT` | agent | optional | `10` |
| `SORTVIEW_HTTP_UPLOAD_READ_TIMEOUT` | agent | optional | `300` |
| `SORTVIEW_HTTP_STATUS_READ_TIMEOUT` | agent | optional | `60` |
| `SORTVIEW_HTTP_RETRY_TOTAL` | agent | optional | `3` |
| `SORTVIEW_HTTP_RETRY_BACKOFF_FACTOR` | agent | optional | `1.0` |
| `SORTVIEW_MAX_RECORDS_PER_REQUEST` | agent | optional | `1000` |
| `SORTVIEW_MAX_LOG_RESPONSE_CHARS` | agent | optional | `500` |
| `SORTVIEW_OUTBOX_DELIVERED_RETENTION_DAYS` | agent | optional | `7` |
| `SORTVIEW_OUTBOX_PRUNE_BATCH_SIZE` | agent | optional | `1000` |
| `SORTVIEW_OUTBOX_MAINTENANCE_MAX_BATCHES_PER_CYCLE` | agent | optional | `10` |
| `SORTVIEW_OUTBOX_MAINTENANCE_INTERVAL_SECONDS` | agent | optional | `3600` |
| `SORTVIEW_OUTBOX_MAINTENANCE_BUSY_TIMEOUT_SECONDS` | agent | optional | `5` |
| `SORTVIEW_OUTBOX_MAX_INCREMENTAL_VACUUM_PAGES` | agent | optional | `1000` |
| `SORTVIEW_OUTBOX_MAINTENANCE_BACKOFF_BASE_SECONDS` | agent | optional | `60` |
| `SORTVIEW_OUTBOX_MAINTENANCE_BACKOFF_MAX_SECONDS` | agent | optional | `3600` |
| `SORTVIEW_OUTBOX_MAINTENANCE_BACKOFF_MULTIPLIER` | agent | optional | `2` |
| `SORTVIEW_API_BASE_URL` | monitoring | required | -- |
| `SORTVIEW_ALERT_EMAIL_TO` | monitoring | optional | unset = no alert emails sent |
| `SORTVIEW_PIPELINE_STALE_MINUTES` | monitoring | optional | `60` |

See [`docs/monitoring.md`](monitoring.md) for how the monitoring target
(GitHub Actions, not the backend or dashboard) is wired up.

The agent also reads per-site, non-secret config (`customer_id`, `branch_id`,
`api_url`, local file paths) from `agent/agent_config.json` on the AMH
machine -- that file is not an environment variable and is not covered by
`.env.example`.

## python version

Local dev, the devcontainer, and CI all target Python 3.11.

The dashboard's production Streamlit Community Cloud deployment does **not**
read a `runtime.txt` for this — that mechanism is currently broken/ignored on
the platform. The actual Python version for that deployment is controlled
only through the "Python version" dropdown in the app's **Advanced Settings**
in the Streamlit Cloud dashboard, which isn't visible from this repo at all.
Before pinning or upgrading a dependency, confirm that dropdown matches 3.11
(or whatever version is intentionally set) rather than assuming it matches
local/CI.
