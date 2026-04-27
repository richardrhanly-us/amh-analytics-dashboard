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

Typical AMH deployment flow:

1. update and validate source in `agent/`
2. copy approved agent files to the AMH machine
3. confirm local config is still correct
4. run `python run_pipeline.py`
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
