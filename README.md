# SortView
[![Launch SortView](https://img.shields.io/badge/Launch-SortView-FF4B4B?logo=streamlit&logoColor=white)](https://sortview.streamlit.app/)

- Username: guest@gmail.com
- Password: guest

SortView is a multi-part system for ingesting, storing, and visualizing Automated Materials Handler activity for a library system.

It currently includes:
- a Windows-side AMH agent that reads local sorter logs and uploads data
- a FastAPI backend that receives uploads and writes to Neon
- a Streamlit dashboard for operational monitoring, reports, transit analysis, and alerts
- Alembic-based database migration tracking for schema changes

The project is designed around New Braunfels Public Library and Tech Logic UltraSort workflows, but the architecture supports broader multi-tenant library operations.

---

## what this repo contains

This repo contains the source-of-truth code for:
- backend API
- Streamlit dashboard
- AMH agent
- database migration files
- tests

The live AMH agent does not run directly from this repo. The deployed copy runs on the AMH-attached Windows machine and is manually updated from the source in agent/.

---

## project structure

    amh-analytics-dashboard/
    ├─ agent/                  # Source-of-truth AMH agent code
    ├─ alembic/                # Alembic migration files
    ├─ src/                    # Main dashboard app code
    ├─ super_admin/            # Super admin pages and auth
    ├─ tests/                  # Automated tests
    ├─ alembic.ini             # Alembic configuration
    ├─ init_db.py              # Transitional DB bootstrap script
    ├─ main.py                 # FastAPI backend for uploads and pipeline status
    ├─ packages.txt            # System packages for deployment environment
    ├─ requirements.txt        # Python dependencies
    └─ README.md

## key folders

### agent

Contains the source-of-truth code for the AMH pipeline agent.

Main files:
- run_pipeline.py
- parse_checkins.py
- parse_rejects.py
- parse_acs.py
- uploader.py
- config.py
- logger_config.py

The deployed agent runs on the AMH-attached Windows machine, typically in:
C:\SortViewAgent

### src

Contains the main dashboard code.

Important areas include:
- app.py
- data_loader.py
- metrics.py
- alerts.py
- transit_logic.py
- reject_logic.py
- views/
- services/
- pages/

### main.py

FastAPI backend entrypoint for:
- /upload
- /upload-pipeline-status

### alembic and alembic.ini

Database migration system for tracked Neon schema changes.

### tests

Automated tests for:
- alerts
- metrics
- transit logic
- parser logic

---

## architecture overview

### AMH agent

The AMH agent runs on the sorter-side Windows machine. It:
- reads local Tech Logic and TLC logs
- parses incremental checkins, rejects, and ACS events
- uploads data to the backend API
- writes local state and status files

### backend API

The backend API:
- authenticates the agent
- receives uploads
- writes data into Neon
- records pipeline status updates

### dashboard

The Streamlit dashboard:
- reads data from Neon
- shows live daily metrics
- shows transit analytics
- shows reject analysis
- shows pipeline health and status
- supports super admin features

### database

Neon is the system-of-record database.

Schema changes are now managed with Alembic.

---

## setup

Create and activate a virtual environment:

    python -m venv .venv
    .venv\Scripts\activate

Install dependencies:

    pip install -r requirements.txt

---

## running the dashboard

    streamlit run src/app.py

---

## running the backend API

The backend entrypoint is:

    python main.py

Actual deployment may use a process manager or platform-specific startup command depending on environment.

---

## running the AMH agent locally

The source-of-truth agent code lives in agent/.

Run the full pipeline locally with:

    python -m agent.run_pipeline

Run parsers individually with:

    python -m agent.parse_checkins
    python -m agent.parse_rejects
    python -m agent.parse_acs

Note: the live production agent runs from the deployed copy on the AMH machine, not directly from this repo.

---

## database migrations

SortView now uses Alembic for schema version tracking.

Check current DB revision:

    alembic current

Create a new migration:

    alembic revision -m "describe the schema change"

Apply migrations:

    alembic upgrade head

The current live database has already been baselined in Alembic.

---

## tests

Run all tests:

    python -m pytest

Run a specific test file:

    python -m pytest tests/test_alerts.py

---

## documentation

Additional documentation should live in docs/, including:
- docs/deployment.md
- docs/database-migrations.md
- docs/agent-deployment.md

---

## operational notes

- Do not run Alembic from the AMH machine
- Do not manage database schema changes in request handlers
- Do not rely on startup-time schema creation for production workflows
- The AMH agent must be manually deployed to the sorter-side machine after validation
- The repo copy in agent/ is the source-of-truth for the agent

---

## status of the project

The project currently includes:
- Neon-backed ingestion
- FastAPI upload endpoints
- Streamlit dashboard views
- pipeline status tracking
- Alembic migration baseline
- agent retry and backoff behavior
- database-first history model

---

## transitional note on init_db.py

init_db.py remains in the repo as a transitional bootstrap and reference script.

Future schema changes should be handled through Alembic migrations rather than expanding init_db.py.

---

## next improvements

Likely future improvements include:
- additional indexed query optimization
- persistent alert history
- richer agent operational metadata
- more formal deployment automation
- additional reports and admin tooling
