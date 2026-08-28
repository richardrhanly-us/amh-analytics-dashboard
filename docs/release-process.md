# release process

This document defines the standard release flow for SortView.

It covers:
- database migrations
- backend API deployment
- dashboard deployment
- AMH agent validation and deployment

The goal is to make releases predictable, repeatable, and low risk.

---

## release order

When a release includes multiple types of changes, use this order:

1. database migration
2. backend API
3. dashboard
4. AMH agent

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

---

## part 1: database migration

Use this section only if the release includes a schema change.

### step 1: confirm Alembic state

From the backend repo root:

    alembic current

Confirm the current revision matches expectations.

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

## part 3: dashboard deployment

Use this section when Streamlit dashboard code changes are included.

### dashboard responsibilities

The dashboard is responsible for:
- loading live and historical data from Neon
- rendering views and KPIs
- displaying pipeline health and status
- providing admin and super admin pages where applicable

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
- super admin pages load if applicable

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

Use this section when agent code changes are included.

### important boundary

The `agent/` folder in the repo is the source-of-truth.

The live AMH agent runs on the AMH-attached Windows machine and is manually updated from the approved repo copy.

### deployment steps

1. validate source changes in `agent/`
2. copy the approved `agent/` folder to the deployed AMH install root,
   keeping it as an `agent/` subfolder there (not flattened) -- every
   module under `agent/` uses package-relative imports, so it only runs
   as a package
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
- do not treat the deployed AMH copy as the long-term source-of-truth
- reconcile changes back to `agent/` in the repo
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
