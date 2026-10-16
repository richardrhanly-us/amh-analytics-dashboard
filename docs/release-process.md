# release process

This document defines the standard release flow for SortView.

It covers:
- database migrations
- backend API deployment
- React admin app deployment
- React customer app deployment
- dashboard (customer Streamlit) deployment
- AMH agent validation and deployment

The goal is to make releases predictable, repeatable, and low risk.

---

## release order

When a release includes multiple types of changes, use this order:

1. database migration
2. backend API
3. React admin app
4. React customer app
5. dashboard (customer Streamlit, while it remains: A5 retires it)
6. AMH agent

This reduces the chance of schema mismatches, API payload mismatches, or stale dashboard assumptions.

---

## before starting a release

Before deploying anything, confirm:

- changes are committed locally
- relevant tests have been run
- the target environment is correct
- the correct database credentials are available
- the change scope is understood:
  - backend only
  - dashboard only
  - migration only
  - agent only
  - or some combination

If the release includes a schema change, review the Alembic migration before doing anything else.

### Collector releases: keeping tests from going stale

`collector.__version__` (`collector/__init__.py`) is the only place the Collector version is written. To release a
new Collector, bump it there by hand -- **no test file should need editing for a version bump**. Ordinary tests
compare against `collector.__version__` (or against what a component reports), never against a copied number.

Two checks enforce this, both fast, dependency-free and run by CI on every pull request (the `lint` job):

- `python scripts/check_test_freshness.py` -- flags a test that compares `collector.__version__` with a literal, asserts
  the current release as a literal, has a release number in its name, or treats a fixed timestamp as "now"
  (a `NOW`/`TODAY` constant) without the shared controlled clock. It also flags production code that hardcodes a
  Collector release as a default -- e.g. a "Collector version" form field, which must import
  `collector.__version__` instead. Historical version fixtures, other kinds of version (schema, API, agent) and
  explicit expiry/boundary timestamps are not flagged. A legitimate exception is annotated where it lives:
  `# freshness: allow FRESH004 -- <reason>` (the reason is required).
- `python scripts/check_release_readiness.py [--expect-version X.Y.Z]` -- also verifies the version authority is
  a single literal with no copies, the packaging/release tooling derives from it, and the time-sensitive enrollment
  tests are wall-clock-free. `--expect-version` is the one place to assert an exact release number: pass the version
  you are about to build.

`python -m collector.build_release` and `collector/freeze/build_frozen.ps1` run the readiness check before building
anything and refuse to continue if it fails.

Time-sensitive tests read one controlled clock (`tests/controlled_clock.py`), generate fixtures from it and reach
"expired" by advancing it -- never by picking a fixed "long ago" date -- so they cannot start failing as real time
passes.

---

## part 1: database migration

Use this section only if the release includes a schema change.

### step 1: confirm Alembic state

From the backend repo root:

    alembic current

Confirm the current revision matches expectations.

Run it with a database role that can read `alembic_version`, i.e. the role that applies migrations. The application's
runtime role (`sortview_app`, which the DigitalOcean backend connects as) is deliberately
given no access to `alembic_version` (`scripts/runtime_role_privileges.py`), and is refused with
`permission denied for table alembic_version`. Do not grant it access to make this check work. From the runtime
environment, `alembic heads` shows only which revision the deployed code expects.

### step 2: create migration if not already created

If the schema change has not yet been recorded:

    alembic revision -m "describe the schema change"

Edit the generated migration file in:

    alembic/versions/

Review both `upgrade()` and `downgrade()` carefully.

### step 3: apply migration

When ready:

    alembic upgrade head

### step 4: validate migration state

Run:

    alembic current

Confirm the target database is now at the expected revision.

### migration validation checklist

After migration:
- expected tables and columns exist
- app code still starts
- no missing-column or missing-table errors appear
- the backend can still connect to Neon
- existing data was preserved as expected

### migration rules

- do not run Alembic from the AMH machine
- do not manage schema changes in request handlers
- do not use ad hoc SQL edits in production as the normal process
- commit migration files to GitHub

---

## part 2: backend API deployment

Use this section when backend code changes are included.

### backend responsibilities

The backend is responsible for:
- receiving `/upload`
- receiving `/upload-pipeline-status`
- authenticating the AMH agent
- writing data into Neon

### deployment steps

1. push backend code changes to GitHub
2. deploy or restart the backend service
3. if the release includes schema changes, make sure Alembic migration has already been run
4. verify the backend starts cleanly

### backend validation checklist

After deployment, verify:
- API starts successfully
- the root health endpoint responds normally
- no startup tracebacks appear
- agent uploads do not return 500 errors
- `pipeline_status` rows continue updating
- recent checkins, rejects, and ACS rows continue appearing in Neon

### recommended post-deploy checks

Check:
- latest `checkins.event_time`
- latest `rejects.event_time`
- latest `acs_events.event_time`
- latest `pipeline_status.updated_at`

If these stop moving unexpectedly after deploy, pause and investigate.

---

## part 2b: React admin app deployment

Use this section when the admin app (`frontend/admin/`), or the customer-app modules it shares, change.

### admin app responsibilities

The admin app is responsible for platform administration at `https://admin.sortviewapp.com`:
- libraries and their overview
- diagnostics
- Collector configuration and Internal Routing
- installations and enrollment codes
- lifecycle
- provisioning

### deployment steps

1. deploy the backend first if the release adds or changes `/api/admin` routes the app calls
2. `npm ci` and `npm run build:admin` from a clean checkout of the release commit; keep `frontend/dist-admin`,
   named by commit, as the rollback artifact
3. upload `assets/` first, then `index.html`, and invalidate `/` and `/index.html`, exactly as in
   `docs/react-hosting-runbook.md`, section L.5

### admin app validation checklist

After deployment, verify the checks in `docs/deployment.md`, "admin app validation checklist". For a rollback, see
`docs/react-hosting-runbook.md`, section L.8.

---

## part 2c: React customer app deployment

Use this section when the customer app (`frontend/`, outside `frontend/admin/`) changes. It is the production customer
entry point at `https://app.sortviewapp.com` (A4, 2026-10-16). The full procedure is
`docs/react-hosting-runbook.md`, section M.

### customer app hosting

- `https://app.sortviewapp.com`: a Cloudflare CNAME, **DNS only**, to the customer CloudFront distribution, with the
  existing ACM certificate (which also covers `admin.sortviewapp.com`)
- the default behavior serves the private S3 bucket (OAC) through the SPA routing function; `/api/*` goes to the
  DigitalOcean backend, uncached

### deployment steps

1. deploy the backend first if the release adds or changes `/api` routes the app calls
2. from `frontend/`, on a clean checkout of the release commit: `npm ci`, then `npm run build`; keep `frontend/dist`,
   named by commit, as the rollback artifact
3. boundary check: the customer `dist/` must not contain `/api/admin` or "SortView Administration" (section M.4)
4. upload `assets/` first with `--cache-control "public,max-age=31536000,immutable"`, then `index.html` with
   `--cache-control "no-cache"`; never use `--delete`
5. invalidate `/` and `/index.html` on the customer distribution

### backend environment

- `https://app.sortviewapp.com` must be in `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS`
- `SORTVIEW_CUSTOMER_APP_URL` must be `https://app.sortviewapp.com`
- `https://admin.sortviewapp.com` stays in `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` (admin sign-in and sign-out) as well as
  in `SORTVIEW_ADMIN_ALLOWED_ORIGINS`
- record the current values privately before any change; saving them redeploys the backend

### customer app validation checklist

After deployment:
- the customer smoke test (`docs/react-hosting-runbook.md`, section M.7): sign-in, refresh, sign-out, a deep route
  loaded directly and refreshed, and the pages the release touched
- client-IP preservation (`docs/react-hosting-runbook.md`, section I) if the distribution's `/api/*` behavior, its
  origin or the backend's hosting changed
- `https://admin.sortviewapp.com` unaffected
- collectors unaffected

### rollback

See `docs/react-hosting-runbook.md`, section M.8. Until A5 retires it, the customer Streamlit app stays available as the
rollback target.

---

## part 3: dashboard deployment

Use this section when Streamlit dashboard code changes are included. Since A4 (2026-10-16) the customer Streamlit app is
not the production customer entry point: it stays live as a temporary rollback target until A5 retires it.

### dashboard responsibilities

The dashboard is responsible for:
- loading live and historical data from Neon
- rendering views and KPIs
- displaying pipeline health and status
- providing the library's admin pages (platform administration is the React admin app; the Super Admin Streamlit
  app was retired in A2 item 8)

### deployment steps

1. push dashboard code changes to GitHub
2. deploy or refresh the Streamlit app
3. verify dashboard loads successfully

### dashboard validation checklist

After deployment, verify:
- app loads without a traceback
- login works
- main branch dashboard loads
- pipeline status panel appears
- latest checkin in DB is not unexpectedly missing
- live today view behaves correctly
- overview and reports pages load

### data validation checks

Check that:
- latest pipeline status is fresh
- latest checkin in DB is reasonable
- live counts are not unexpectedly zero
- historical views still populate
- tenant schema validation does not fail

If the dashboard looks blank or stale, compare dashboard loader table names to the actual API write targets in Neon.

---

## part 4: AMH agent deployment

**This section describes the CURRENTLY-DEPLOYED production agent only** --
the archived snapshot under
`agent/SortViewAgent - What is currently sitting on the AMH computer/`,
not the top-level `agent/` package. A new canonical continuous agent
(`agent/main.py` + `agent/runtime/*`) has been built and tested in this
repo but has **not been deployed to or validated on the real AMH
machine** -- see `agent/README.md` for its status and remaining gates.
Do not use this section to deploy that new agent; that is a separate,
later, explicit cutover decision.

Use this section when changes to the *currently deployed* agent logic
are included.

### important boundary

The archived snapshot folder above is the source-of-truth reference for
what is actually running on the AMH machine today -- it is preserved
read-only in the repo specifically so there's never ambiguity about what
production behavior looked like. `agent/run_pipeline.py` (top-level, no
archive path) is a separate, legacy/validation-only local mirror kept for
side-by-side comparison during later validation; it is not what's
deployed.

The live AMH agent runs on the AMH-attached Windows machine and is manually updated from the approved archived-snapshot copy.

### deployment steps

1. validate source changes against the archived snapshot folder
2. copy the approved agent folder to the deployed AMH install root,
   keeping it as an `agent/` subfolder there (not flattened) -- every
   module uses package-relative imports, so it only runs as a package
3. confirm `agent_config.json` is still correct
4. open Command Prompt on the AMH machine, in the install root (the
   parent of the deployed `agent/` folder -- not inside `agent/` itself)
5. run:

    python -m agent.run_pipeline

6. review output carefully
7. if healthy, return the scheduled task or normal execution process to service

### expected healthy output

A healthy validation run usually includes:
- pipeline run started
- start status upload returned 200
- parsing completed without exceptions
- upload batch returned 200
- completed status upload returned 200
- pipeline state file updated
- pipeline run completed successfully

### agent validation checklist

After deployment, verify:
- parser runs complete without exceptions
- offsets update correctly
- upload returns HTTP 200
- completed status returns HTTP 200
- new rows appear in Neon
- dashboard reflects the new data

### agent deployment rules

- do not run Alembic from the AMH machine
- if the deployed AMH copy and the archived snapshot folder in the repo
  ever diverge, treat the archived snapshot as the version to reconcile
  against
- do not edit the archived snapshot folder directly -- it is preserved
  read-only historical evidence, not a working copy (see its own README)
- do not commit runtime output files as part of normal code release workflow

---

## full release checklist

Use this when a release affects more than one layer.

### pre-release
- confirm scope of change
- confirm target environment
- confirm working tree is clean or intentionally staged
- confirm migration needs
- confirm tests were run

### migration
- run `alembic current`
- apply `alembic upgrade head` if needed
- verify migration result

### backend
- deploy backend
- verify API health
- verify upload behavior

### admin app
- build and upload `dist-admin` if included (assets first, `index.html` last, then invalidate)
- verify sign-in, a deep route and the origin guard

### dashboard
- deploy dashboard
- verify pages and live data
- verify latest pipeline status

### agent
- deploy agent changes if included
- run `python -m agent.run_pipeline` from the AMH install root (not from inside `agent/`)
- verify successful upload and status writes

### post-release
- confirm recent DB rows are moving
- confirm dashboard reflects current DB state
- confirm no unexpected errors in logs
- document any issues or rollback actions taken

---

## rollback mindset

Not every release needs a full rollback plan, but every release should consider failure boundaries.

Examples:
- migration failure
- backend starts but upload fails
- dashboard loads but shows stale data
- AMH agent runs but payload no longer matches backend expectations

When something goes wrong:
1. stop making additional changes
2. identify which layer failed
3. restore last known-good code if needed
4. verify database state before applying more fixes
5. retest in the standard release order

---

## operational reminders

- Neon is the system-of-record database
- Alembic is now the official schema-tracking path
- `init_db.py` has been retired; the Alembic baseline migration is the schema evolution process
- the AMH agent is deployed manually even though its source-of-truth lives in the repo
- future improvements can automate parts of this process, but the documented order should remain the same
