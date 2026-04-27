# SortView AMH Agent

This folder is the source of truth for the SortView AMH agent.

The live agent does not run from this repo directly. The deployed copy runs on the AMH-attached Windows machine, typically in:

`C:\SortViewAgent`

Changes made in this folder should be validated and then manually deployed to the AMH machine.

## Purpose

The AMH agent is responsible for:

- reading Tech Logic / TLC source logs from the local AMH machine
- parsing incremental checkins, rejects, and ACS events
- uploading those rows to the SortView API
- writing local pipeline state and status files used by the deployed agent

## Core runtime files

These are the main files that make up the deployed agent:

- `run_pipeline.py`
- `parse_checkins.py`
- `parse_rejects.py`
- `parse_acs.py`
- `uploader.py`
- `config.py`
- `logger_config.py`

## Configuration

The deployed agent uses a local `agent_config.json` on the AMH machine for runtime settings such as:

- API URL
- customer and branch IDs
- raw log file paths
- processed output file paths
- status file path

Secrets such as database or API credentials should not be hardcoded into the agent source.

## Deployment notes

This repo stores the source-of-truth copy of the agent, but deployment is manual.

Typical update process:

1. make and validate code changes in this repo
2. copy the approved agent files to the AMH machine
3. run `python run_pipeline.py` locally on the AMH machine
4. confirm:
   - upload succeeds
   - pipeline status writes succeed
   - offsets update correctly
   - no parser or runtime errors appear
5. return the scheduled task or normal execution process to service

## Important boundaries

This folder is only for the AMH agent.

It does not manage:

- FastAPI backend routes
- Streamlit dashboard code
- Neon schema migrations
- Alembic configuration

Those belong in the main backend/dashboard areas of the repo.

## Database and migrations

The agent sends data to the API. It does not manage the database schema directly.

Database schema changes should be handled through the backend migration workflow, not by modifying the agent.

## Notes

If the deployed AMH copy and the repo copy ever differ, this folder should be treated as the source-of-truth version to reconcile against.
