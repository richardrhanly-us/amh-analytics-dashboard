# uptime and pipeline health monitoring

Before this, the only signal that the backend was down or an AMH agent had
stopped uploading was a human looking at the dashboard's pipeline-status
panel and noticing something looked stale. This document covers the two
automated checks that replace "waiting for a user to notice."

## what's checked

Two independent checks, each its own script, each its own job in the same
GitHub Actions workflow (`.github/workflows/uptime-monitoring.yml`):

- **Backend uptime** (`scripts/check_uptime.py`) -- `GET`s the deployed
  backend API's `/` endpoint and expects `200 OK`. Runs from GitHub Actions,
  not from the backend itself, because a health check the backend runs on
  itself can't tell you anything once the backend is actually down.

- **Pipeline health** (`scripts/check_pipeline_health.py`) -- queries
  `pipeline_status` for every branch with `branches.status = 'active'` and
  flags a branch if any of the following is true:
  - it has no `pipeline_status` row at all (never reported),
  - its `updated_at` is older than `SORTVIEW_PIPELINE_STALE_MINUTES`, or
  - its latest `status` starts with `failed`.

Both checks run on the same schedule (every ~15 minutes, best-effort -- see
"known limitations" below) and can also be triggered on demand from the
Actions tab (`workflow_dispatch`).

## alerting

Both scripts send at most one email per run, to the comma-separated list in
`SORTVIEW_ALERT_EMAIL_TO`, using the same SMTP configuration already wired
up for password-reset emails (`SORTVIEW_SMTP_HOST` / `_PORT` / `_USERNAME` /
`_PASSWORD`, `SORTVIEW_EMAIL_FROM`). No new email infrastructure.

If `SORTVIEW_ALERT_EMAIL_TO` isn't set, both checks still run and still
exit non-zero on failure -- they just skip sending mail. The workflow run
itself turning red in the Actions tab is the fallback signal either way.

## setup

All of these live in the repository's Actions secrets/variables (Settings ->
Secrets and variables -> Actions), since this is the first workflow in this
repo that reaches real production infrastructure from CI:

**Secrets:**

- `SORTVIEW_API_BASE_URL` -- the deployed backend's public URL
- `DATABASE_URL` -- same Neon connection string used everywhere else
- `SORTVIEW_ALERT_EMAIL_TO`, `SORTVIEW_SMTP_HOST`, `SORTVIEW_SMTP_PORT`,
  `SORTVIEW_SMTP_USERNAME`, `SORTVIEW_SMTP_PASSWORD`, `SORTVIEW_EMAIL_FROM`

**Variables** (not secret, but easier to tune from the Actions UI than a
code change):

- `SORTVIEW_PIPELINE_STALE_MINUTES` -- see the tuning note below

Full description of each variable is in [`.env.example`](../.env.example)
and the table in [`deployment.md`](deployment.md#environment-variables).

### tuning `SORTVIEW_PIPELINE_STALE_MINUTES`

There's no correct default here -- it depends entirely on how often each
branch's AMH agent is actually scheduled to run, which lives in a Windows
Task Scheduler entry on that branch's machine, not in this repo. Set it
comfortably longer than that interval (including normal retry/backoff time
on a flaky connection), or this will alert on every routine gap between
runs instead of an actual outage. `60` is a placeholder, not a
recommendation -- confirm the real schedule before trusting the default.

## testing it

Run either script locally against real config to see it work before relying
on the scheduled job:

```bash
SORTVIEW_API_BASE_URL=https://your-backend-url python scripts/check_uptime.py

DATABASE_URL=postgresql://... SORTVIEW_PIPELINE_STALE_MINUTES=60 \
    python scripts/check_pipeline_health.py
```

Both print what they found and exit `0` on success / `1` on any problem,
whether or not `SORTVIEW_ALERT_EMAIL_TO` is set. To see the alert email
itself fire, point `SORTVIEW_API_BASE_URL` at something that will actually
fail (an unreachable host, a wrong port) or temporarily lower
`SORTVIEW_PIPELINE_STALE_MINUTES` below a real branch's last report time.

The workflow can also be run on demand from the GitHub Actions tab
(`workflow_dispatch`) without waiting for the schedule.

## known limitations

- **GitHub Actions schedules are best-effort.** The `*/15 * * * *` cron can
  run several minutes late under GitHub's load, and GitHub automatically
  disables a scheduled workflow after 60 days with no commits to the
  repository (any push re-enables it). This is not a substitute for a real
  uptime monitor with SLA-backed check intervals if that's ever needed.
- **Both checks depend on GitHub Actions being able to reach the backend
  and Neon.** If GitHub Actions itself has an outage, or an IP allowlist on
  either service blocks GitHub's runners, checks silently stop running
  rather than alerting.
- **No escalation.** One email per run, no retry/backoff on the alert
  itself, no paging, no on-call rotation. If the alert email bounces or
  lands in spam, nothing else notices.
- **No dashboard for check history.** Results live in the Actions run log
  for whatever GitHub's retention window is -- there's no persisted
  timeline of past incidents.
