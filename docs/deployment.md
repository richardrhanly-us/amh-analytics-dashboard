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
