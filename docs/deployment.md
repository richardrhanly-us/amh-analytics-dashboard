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

**This section describes the currently-deployed production process only.** The actual, running-today production agent is the archived snapshot under
`agent/SortViewAgent - What is currently sitting on the AMH computer/` --
a separate, self-contained copy with its own `run_pipeline.py`,
`config.py`, etc., installed at `C:\SortViewAgent` on the AMH machine and
invoked by a 15-minute Task Scheduler entry. It has not been touched by
this repo's continuous-ingestion redesign work and is not affected by
anything below.

A new canonical continuous agent (`agent/main.py` + `agent/runtime/*`)
has since been built and tested in this repo, and will eventually replace
the process above -- but it has **not yet been deployed or validated on
the real AMH machine**. See `agent/README.md` for what it is, how to run
it locally, and exactly what pre-production validation remains before
that cutover can happen. Do not deploy it to the AMH machine, disable the
existing Scheduled Task, or otherwise change the real machine based on
this document -- that is a separate, later, explicit decision.

`agent/run_pipeline.py` (top-level, inside this repo's `agent/` package)
is a legacy/validation-only local mirror of the archived baseline's
logic, kept only as a runnable comparison point for later side-by-side
validation -- it is not what's deployed today and not the canonical
replacement either. See `agent/README.md` for the full picture of what's
what.

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
| `SORTVIEW_ENROLL_RATE_LIMIT` | backend | optional | `10/minute` (per client address, `POST /collector/enroll`) |
| `SORTVIEW_MAX_REQUEST_BODY_BYTES` | backend | optional | `5242880` (5 MB) |
| `SORTVIEW_ALLOWED_ORIGINS` | backend | optional | `http://localhost:8501,http://127.0.0.1:8501` |
| `SORTVIEW_API_DOCS_ENABLED` | backend | optional | `false` (`/docs`, `/redoc` and `/openapi.json` are not served; set `true` for local development only) |
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

## streamlit error display

Streamlit's default (`client.showErrorDetails = "full"`) shows an uncaught exception's type, message
and traceback in the browser. A database driver's message quotes the SQL, bound values, connection
string and failing row, so the repository pins the setting in
[`.streamlit/config.toml`](../.streamlit/config.toml):

```toml
[client]
showErrorDetails = "none"
```

Users then see only "This app has encountered an error". This is covered by
`tests/test_streamlit_error_details.py`.

- **The file only applies when Streamlit is started from the repository root** (`streamlit run
  src/app.py`, as in the README and devcontainer; the Super Admin app likewise). Streamlit reads
  `$CWD/.streamlit/config.toml`, not a path relative to the script, so a launch from another directory
  silently falls back to `"full"`.
- **It can be overridden from outside the repository -- and Streamlit Community Cloud does.** An environment
  variable (`STREAMLIT_CLIENT_SHOW_ERROR_DETAILS`) or a `--client.showErrorDetails` flag given to `streamlit run`
  outranks the file. Community Cloud
  [documents](https://docs.streamlit.io/deploy/streamlit-community-cloud/status) that it forces the legacy
  `client.showErrorDetails = false` at startup regardless of `config.toml`; the hosted canary check confirmed the
  effective value was `"false"`. In Streamlit 1.63.0 `false` is `"stacktrace"`: the message is redacted, but the
  exception **type** and the **traceback** (server file paths and source lines) still reach the browser.
- **So SortView enforces `"none"` in code, unconditionally.** `install_streamlit_log_scrubber()` calls
  `enforce_streamlit_error_details()` (`src/services/privacy_hardening.py`), which sets `client.showErrorDetails`
  to `"none"` through Streamlit's supported `st.set_option` on every script run, after whatever the platform, an
  environment variable or a flag set at startup. There is deliberately **no opt-out**: `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=full`
  no longer turns details back on in the apps. To debug an exception locally use the tests or a debugger. The
  `config.toml` setting stays as the repository-level default; the code pin is in addition to it. If the pin cannot
  be applied the app logs one fixed line, `Could not enforce Streamlit client.showErrorDetails=none`, and carries on.
  Tests: `tests/test_streamlit_error_details_enforcement.py`, `tests/test_real_apps_redaction.py`,
  `tests/test_redaction_canary_app.py`. Verified on Streamlit Community Cloud (2026-09-21, hosted canary): Cloud's
  startup value was `false`, SortView's runtime enforcement changed the effective value to `none`, and the hosted
  verification passed (runbook, 2.6).
- **Streamlit's server log is scrubbed separately.** `showErrorDetails` only controls the browser: Streamlit
  always logs the whole uncaught exception, message included, before deciding what the browser sees (on a
  `requirements.txt`-only install such as production, as `Uncaught app execution` on stderr; where the optional
  `rich` package is installed, as a console print to stdout). Every Streamlit entry script therefore calls
  `install_streamlit_log_scrubber()` (`src/services/privacy_hardening.py`) first, which rewrites those log
  records to the exception type, SQLSTATE and code location -- never the message -- and turns the `rich`
  print off. `tests/test_streamlit_log_scrubbing.py` covers it, including a guard that fails if a new entry
  script (`src/app.py`, `src/pages/*`, `super_admin/**`) does not install it.
- **What that does not cover:** output written before the scrubber is installed in a process's first script run
  (for example an import failure), text written by other means (`print`, other libraries' own loggers, the
  hosting platform's infrastructure logs), and the message arguments of Streamlit log lines that are not
  exception records. Treat the hosting log as sensitive regardless.
- **After each deployment, verify it on the live app** (this cannot be checked from the repo). The full,
  step-by-step procedure -- a standalone canary app plus copies of the two real apps, with pass/fail criteria
  and log search strings -- is in [`production-verification-runbook.md`](production-verification-runbook.md), Part 2.
  In short: make a page raise a deliberate error in a non-production copy, confirm the page shows only the generic message,
  then read the hosting log ("Manage app" on Streamlit Cloud) and confirm it shows
  `Uncaught app execution | error_type=... at=...` and NOT the exception's message. Also check that the
  canary's panel shows the platform's startup value (`false` on Community Cloud) and the enforced `none`, and that
  the Super Admin app is started from the repository root.

## admin settings password

The Admin Settings page asks an owner/admin for an extra "admin password" before it shows the settings form.
It is per organization, is not the login system (`app_users` has its own hashed passwords), and protects only
that page. It is stored in `organization_settings.settings_json` under `security` as a salted one-way hash
(`admin_password_hash`, the same werkzeug scheme as user passwords); the page never pre-fills it and the
dashboard's cached settings never contain the `security` block.

- **Older organizations may still hold a plaintext `security.admin_password`.** It keeps working (so nobody is
  locked out) and the page shows a notice; the next time an owner/admin saves Admin Settings the plaintext is
  replaced by a hash of the same password. Until then it remains in that organization's `settings_json`.
  `scripts/admin_lock_inventory.sql` (read-only) finds such rows and `scripts/migrate_admin_lock_hashes.py`
  (dry run by default) converts them in one controlled step; see
  [`production-verification-runbook.md`](production-verification-runbook.md), Part 1.
- **Rolling back to a version before this change** reads only `admin_password`, so an organization whose
  password has already been converted would have no lock until an admin sets one again.
- The lock has no attempt limit; it is a second prompt behind a signed-in owner/admin, not a login.
