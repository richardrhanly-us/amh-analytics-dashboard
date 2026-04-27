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

`init_db.py` may still exist as a transitional or bootstrap reference, but future schema evolution should go through Alembic.

## where Alembic lives

Alembic belongs to the backend repo.

Relevant files:

- `alembic.ini`
- `alembic/`
- `alembic/versions/`

Alembic should be run from the backend development or deployment environment, not from the AMH agent machine.

## prerequisites

Before running Alembic commands:

- open a terminal in the backend repo root
- make sure `DATABASE_URL` is set in that terminal session
- make sure the target database is the correct Neon environment

## check current revision

Use:

    alembic current

## create a new migration

Use:

    alembic revision -m "describe the schema change"

This creates a new migration file in:

`alembic/versions/`

Then edit the generated migration file to describe the schema change.

## apply migrations

Use:

    alembic upgrade head

This applies all pending migrations to the target database.

## baseline note

The initial baseline migration is intentionally empty and was used only to mark the current production schema as tracked.

That baseline was applied with:

    alembic stamp head

This was done because the existing Neon schema already existed before Alembic was introduced.

## safe workflow for future changes

Typical migration workflow:

1. set `DATABASE_URL`
2. run `alembic current`
3. create a migration with `alembic revision -m "..."`
4. edit the migration file
5. review the migration carefully
6. run `alembic upgrade head`
7. verify app behavior after the migration

## examples of valid schema changes

Typical future migrations may include:

- adding a new table
- adding a new column
- adding an index
- changing nullability
- renaming a column in a controlled migration
- backfilling data in a migration when necessary

## important rules

- do not run Alembic from the AMH machine
- do not manage schema changes in FastAPI request handlers
- do not use ad hoc SQL changes in production as the normal path
- do not hardcode the real `DATABASE_URL` into repo files
- commit migration files to GitHub

## rollback note

Not every migration is easy to reverse automatically. Treat downgrade logic carefully.

Before applying significant migrations to production, review:

- what objects are changing
- whether data could be lost
- whether a backup or rollback plan is needed

## operational reminder

The database stores Alembic revision state in its Alembic version table, while the migration files live in GitHub.

That means recovery from a lost local machine is straightforward as long as:

- migration files are committed to the repo
- the correct database credentials are available
