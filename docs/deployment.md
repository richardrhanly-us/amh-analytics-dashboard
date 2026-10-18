# deployment

This document explains how to deploy and validate the main parts of SortView.

## system parts

SortView currently has four operational parts:

- backend API (FastAPI on DigitalOcean)
- React customer app, `https://app.sortviewapp.com` (the customer UI)
- React admin app, `https://admin.sortviewapp.com` (platform administrators only)
- AMH agent / SortView Collector

These parts are related, but they are deployed differently.

The React customer app (`frontend/`) has been the production customer UI at `https://app.sortviewapp.com` since the
A4 cutover (2026-10-16), and is the only customer UI: the Streamlit dashboard was retired in A5 (hosted app deleted,
source and dependencies removed). Both React apps' hosting is in [react-hosting-runbook.md](react-hosting-runbook.md):
section M covers the production customer app (sections A to K its original trial setup), and section L the production
admin app.

## backend API deployment

The backend API is responsible for:

- receiving `/upload`
- receiving `/upload-pipeline-status`
- authenticating the AMH agent
- writing data into Neon
- serving the customer (browser) API under `/api`: session login and logout, the
  signed-in user's organization context, and five read-only operational endpoints
  (the former `ingest-status` route is gone: a site's Contract v2 collector heartbeat detail is
  served only to platform administrators, under `/api/admin`):
  - `GET /api/organizations/{org_slug}/branches/{branch_slug}/pipeline-status`
    (what the branch's collection pipeline last reported: `state` is one of `ok`,
    `degraded`, `failed` or `unknown`; `last_reported_at` is the UTC instant that report
    was received, or `null` if nothing has been reported; `timezone` is
    `SORTVIEW_LIVE_TIMEZONE`. The state describes the last report only -- the endpoint
    makes no judgement of how old that report is, so a reader must compare
    `last_reported_at` with the current time)
  - `GET /api/organizations/{org_slug}/branches/{branch_slug}/checkins/count?date=YYYY-MM-DD`
    (check-ins on one calendar day; `date` is required and is interpreted in
    `SORTVIEW_LIVE_TIMEZONE`, which the response echoes)
  - `GET /api/organizations/{org_slug}/branches/{branch_slug}/checkins/by-hour?date=YYYY-MM-DD`
    (the same day's check-ins by hour; `date` is required and is interpreted in
    `SORTVIEW_LIVE_TIMEZONE`, and the response always contains 24 local wall-clock
    buckets, hours 0 to 23, whose counts add up to the `checkins/count` result)
  - `GET /api/organizations/{org_slug}/branches/{branch_slug}/rejects/count?date=YYYY-MM-DD`
    (rejects on one calendar day; `date` is required and is interpreted in
    `SORTVIEW_LIVE_TIMEZONE`, and the response returns the raw count of reject rows
    stored for that local date, whatever their reason)
  - `GET /api/organizations/{org_slug}/branches/{branch_slug}/rejects/by-reason?date=YYYY-MM-DD`
    (the same day's rejects by reason; `date` is required and is interpreted in
    `SORTVIEW_LIVE_TIMEZONE`, and the response always contains the eight reason codes
    in a fixed order, zero where there were none, whose counts add up to the
    `rejects/count` result)

Typical backend deployment flow:

1. make code changes locally
2. commit and push to GitHub
3. deploy the backend service
4. if schema changes are included, run Alembic migrations
5. verify API health

## backend validation checklist

After backend deployment, verify:

- the API starts successfully
- `/` responds normally, with the API security headers (`Strict-Transport-Security: max-age=31536000` among them;
  [react-hosting-runbook.md](react-hosting-runbook.md), section N)
- agent uploads do not return 500 errors
- `pipeline_status` rows continue updating
- recent checkins/rejects/ACS rows continue appearing in Neon

## React customer app deployment

The customer app (`frontend/`) is served from its own private S3 bucket through its own CloudFront distribution at
`https://app.sortviewapp.com`. The distribution's `/api/*` behavior sends API requests to the backend above.

Deployment is manual:

1. build `npm run build` from a clean checkout of the commit being deployed (output `frontend/dist`), and check the
   bundle contains no admin code;
2. upload `assets/` (cached for a year), then `index.html` (`no-cache`);
3. invalidate `/` and `/index.html`.

The exact commands, the CloudFront settings, the backend environment it needs and the rollback are in
[react-hosting-runbook.md](react-hosting-runbook.md), section M. The production verification is in
[production-verification-runbook.md](production-verification-runbook.md), Part 4.

## customer app validation checklist

After a customer deployment, verify:

- `https://app.sortviewapp.com` loads, and a direct load or refresh of a deep route (e.g. a sorter's reports) shows the app
- a customer user can sign in, refresh and sign out
- `/api/auth/session` answers 401 JSON when signed out (never the app's HTML)
- a sorter's Live Today and Reports load with current data
- `admin.sortviewapp.com` and collector uploads are unaffected

## React admin app deployment

The admin app (`frontend/admin/`) is served from its own private S3 bucket through its own CloudFront distribution
at `https://admin.sortviewapp.com`. The distribution's `/api/*` behavior sends API requests to the backend above.

Deployment is manual:

1. build `npm run build:admin` from a clean checkout of the commit being deployed (output `frontend/dist-admin`);
2. upload `assets/` (cached for a year), then `index.html` (`no-cache`);
3. invalidate `/` and `/index.html`.

The exact commands, the CloudFront settings, the backend environment it needs and the rollback are in
[react-hosting-runbook.md](react-hosting-runbook.md), section L. The production verification is in
[production-verification-runbook.md](production-verification-runbook.md), Part 3.

The backend must list the admin origin in **both** `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` (sign-in and sign-out) and
`SORTVIEW_ADMIN_ALLOWED_ORIGINS` (admin writes); see "environment variables" below.

## admin app validation checklist

After an admin deployment, verify:

- `https://admin.sortviewapp.com` loads, and a direct load or refresh of a deep route (e.g. `/libraries/<slug>`) shows the app
- a platform administrator can sign in, navigate and sign out
- `/api/admin/session` answers 401 JSON when signed out (never the app's HTML)
- a POST with a forged `Origin` is refused with 403 `origin_not_allowed`
- the customer app and collector uploads are unaffected

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
- the customer app's Live Today reflects new data

## deployment order when multiple parts change

If multiple parts change at once, use this order:

1. database migration
2. backend API
3. React admin app (after the backend routes it calls are deployed)
4. React customer app (after the backend routes it calls are deployed)
5. AMH agent

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
the DigitalOcean app's environment for the backend, the AMH machine's
scheduled task environment for the agent, CI secrets for the pipeline.

| Variable | Target | Required? | Default |
|---|---|---|---|
| `DATABASE_URL` | backend | required | -- (read from the environment only) |
| `SENTRY_DSN` | backend | optional | unset = Sentry disabled |
| `SENTRY_ENVIRONMENT` | backend | optional | `development` |
| `SORTVIEW_UPLOAD_RATE_LIMIT` | backend | optional | `30/minute` |
| `SORTVIEW_ENROLL_RATE_LIMIT` | backend | optional | `10/minute` (per client address, `POST /collector/enroll`) |
| `SORTVIEW_MAX_REQUEST_BODY_BYTES` | backend | optional | `5242880` (5 MB) |
| `SORTVIEW_ALLOWED_ORIGINS` | backend | optional | `http://localhost:8501,http://127.0.0.1:8501` |
| `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` | backend | required for browser login | empty = every state-changing `/api` request is refused (comma-separated exact origins, e.g. `https://app.example.com`). The separate admin app signs in and out through `/api/auth/login` and `/api/auth/logout`, so its origin must be listed here as well as in `SORTVIEW_ADMIN_ALLOWED_ORIGINS`. **Production requires** `https://app.sortviewapp.com`, `https://admin.sortviewapp.com` **and** the customer distribution's generated hostname `https://d22i2vjcxygt0c.cloudfront.net` (see [react-hosting-runbook.md](react-hosting-runbook.md), M.5) |
| `SORTVIEW_ADMIN_ALLOWED_ORIGINS` | backend | required for admin writes | empty = every state-changing `/api/admin` request is refused (comma-separated exact origins of the separate admin app; GET needs no Origin). Kept apart from the customer list so a page on the customer app's origin can never send an admin write. See `src/admin_api/` |
| `SORTVIEW_CUSTOMER_COOKIE_SECURE` | backend | optional | `true` (only the exact value `false` turns `Secure` off, for local HTTP development) |
| `SORTVIEW_LOGIN_RATE_LIMIT` | backend | optional | `10/minute` (per client address, `POST /api/auth/login`) |
| `SORTVIEW_CUSTOMER_APP_URL` | backend | required for password reset from the customer app | -- (the origin people open the customer app at, e.g. `https://app.example.com`: no path, and it must be one of `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS`. Reset emails requested through `/api` link to `<this>/reset-password#token=...` -- the token is in the URL fragment, which browsers do not send to a server, so it is not in access logs or `Referer` headers. Unset or not allowed = `POST /api/auth/password-reset/request` answers 503 for every address) |
| `SORTVIEW_PASSWORD_ATTEMPT_RATE_LIMIT` | backend | optional | `10/minute` (per client address; `POST /api/account/change-password` and `POST /api/auth/password-reset/complete`, each its own bucket) |
| `SORTVIEW_PASSWORD_RESET_REQUEST_RATE_LIMIT` | backend | optional | `5/minute` (per client address, `POST /api/auth/password-reset/request`) |
| `SORTVIEW_API_DOCS_ENABLED` | backend | optional | `false` (`/docs`, `/redoc` and `/openapi.json` are not served; set `true` for local development only) |
| `SORTVIEW_SMTP_HOST` | backend, monitoring | required for password reset | -- (the backend needs the four required SMTP settings to send reset emails requested through `/api`; if any is missing, the request endpoint answers 503. Monitoring uses the same settings for alert emails) |
| `SORTVIEW_SMTP_PORT` | backend, monitoring | optional | `587` |
| `SORTVIEW_SMTP_USERNAME` | backend, monitoring | required for password reset | -- |
| `SORTVIEW_SMTP_PASSWORD` | backend, monitoring | required for password reset | -- |
| `SORTVIEW_EMAIL_FROM` | backend, monitoring | required for password reset | -- |
| `SORTVIEW_LIVE_TIMEZONE` | backend | optional | `America/Chicago` (must be a valid IANA zone; the customer operational metrics API answers 500 if it is set to an invalid one) |
| `SORTVIEW_API_TOKEN` | agent (legacy `agent\` runtime only -- **not** the SortView Collector, see below) | required | -- |
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
(GitHub Actions, not the backend) is wired up.

The agent also reads per-site, non-secret config (`customer_id`, `branch_id`,
`api_url`, local file paths) from `agent/agent_config.json` on the AMH
machine -- that file is not an environment variable and is not covered by
`.env.example`.

### SortView Collector API token (not an environment variable)

The SortView Collector (`collector\`, the deployed v1 runtime) does **not**
take its token from the environment. Its only token source is

    <DataRoot>\secrets\api_token.dpapi

(`C:\ProgramData\SortViewCollector\secrets\api_token.dpapi` by default): a
DPAPI machine-scope blob, bound to the config's `customer_id`/`branch_id`, in
a folder restricted to Administrators and SYSTEM. From Collector 1.0.12 the
one-release migration fallback is gone: `SORTVIEW_API_TOKEN` is never read by
the Collector runtime or by any of its install/finish/update scripts, and a
Collector with no usable `api_token.dpapi` fails closed.

- Store or rotate: `tools\set-api-token.ps1` (elevated; SecureString prompt).
- Verify: `SortViewCollector.exe api-token check --config <ConfigPath>` --
  exit `0` is the only state in which the Collector runs.
- Update prerequisite: `api-token check` must exit `0` **before** updating a
  machine to 1.0.12 or later. The updaters preserve `api_token.dpapi` but
  never create or migrate one; without it the new runtime's preflight fails
  and the Scheduled Task is left disabled.

Removing a leftover Machine-scope variable is a separate, explicit operator
action -- no Collector script does it. Do it only after `api-token check`
exits `0`, a Collector run has succeeded, and nothing else on the machine
(for example the legacy `agent\` runtime) still depends on the variable:

    [Environment]::SetEnvironmentVariable("SORTVIEW_API_TOKEN", $null, "Machine")

See `docs/collector-v1-admin-guide.md` (*Token setup*, *Update*).

## python version

Local dev, the devcontainer, and CI all target Python 3.11.

The production backend installs `requirements.txt` on DigitalOcean, whose Python version is set there and is not
visible from this repository. Before pinning or upgrading a dependency, confirm the version production actually runs
rather than assuming it matches local/CI.
