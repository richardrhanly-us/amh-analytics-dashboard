# database migrations

This document explains how SortView manages Neon database schema changes.

## current approach

SortView now uses Alembic for database schema version tracking.

The current live database has been baselined at:

`26397a3947b1`

This means Alembic recognizes the current production schema as the starting point for future tracked changes.

## what Alembic replaces

Alembic replaces the old pattern of managing schema changes through:

- request-time table creation
- startup-time schema creation
- one-off schema edits in application code

`init_db.py` may still exist as a transitional/bootstrap reference, but future schema evolution should go through Alembic.

## where Alembic lives

Alembic belongs to the backend repo.

Relevant files:

- `alembic.ini`
- `alembic/`
- `alembic/versions/`

Alembic should be run from the backend development/deployment environment, not from the AMH agent machine.

## prerequisites

Before running Alembic commands:

- open a terminal in the backend repo root
- make sure `DATABASE_URL` is set in that terminal session
- make sure the target database is the correct Neon environment

## check current revision

Use:

```bat
alembic current
