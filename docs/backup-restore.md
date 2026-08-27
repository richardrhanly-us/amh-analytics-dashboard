# backup and restore

This document covers how SortView's Neon database is recovered after data
loss (a bad `DELETE`, a broken migration, accidental data corruption). It
was written after actually running the drill described below end-to-end
against a real Neon project, not just from reading Neon's docs.

## what Neon actually provides

Neon does not do traditional nightly `pg_dump`-style backups. Its recovery
mechanism is **point-in-time restore (PITR)**, backed by the same
copy-on-write storage that powers branching. Any branch can be:

- **branched** as of a past timestamp (creates a new, separate branch --
  non-destructive, the original branch is untouched), or
- **restored** to a past timestamp (resets an _existing_ branch's data back
  to that point in time -- this is what you want when the branch you're
  recovering, e.g. `production`, needs to go back in time itself)

Both were tested below and both work as documented.

### retention window

PITR only works within your project's retention window. This is **not a
fixed number** -- it depends on the Neon plan and is set per project. Check
the real value before assuming how far back you can go:

```bash
curl -s "https://console.neon.tech/api/v2/projects/$NEON_PROJECT_ID" \
  -H "Authorization: Bearer $NEON_API_KEY" \
  | python -c "import json,sys; print(json.load(sys.stdin)['project']['history_retention_seconds'])"
```

At the time this was written, the disposable `SortTEST` project used for
the initial drill had a 21600-second (6-hour) retention window.

The production SortView project was subsequently verified in the Neon
Console on 2026-08-27 and has a **1-day history retention window**.

Operationally, this means Neon PITR can recover production data only when
the required restore point remains within that one-day window. Incidents
that are not discovered until after the retention window may require an
off-platform backup, so PITR should not be treated as the eventual sole
backup strategy for a commercial deployment.

## prerequisites

- A Neon **API key** (Neon Console -> Account Settings -> API Keys). Branch
  creation/restore happens at Neon's control-plane/API level, not through a
  plain Postgres connection -- a `DATABASE_URL` alone isn't enough to do a
  restore, only to inspect/verify one.
- The Neon **project ID** (project Settings page, or `neon projects list`
  with the CLI).
- The **branch ID** you're restoring (Neon Console, or:
  `curl .../projects/$NEON_PROJECT_ID/branches`).

## procedure A: branch to a point in time (non-destructive)

Use this to inspect or recover specific rows without touching the live
branch -- e.g. "what did this table look like an hour ago."

```bash
curl -s -X POST "https://console.neon.tech/api/v2/projects/$NEON_PROJECT_ID/branches" \
  -H "Authorization: Bearer $NEON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "branch": {
      "name": "recovery-check",
      "parent_id": "'"$SOURCE_BRANCH_ID"'",
      "parent_timestamp": "2026-08-27T12:20:36.355009+00:00"
    },
    "endpoints": [ { "type": "read_write" } ]
  }'
```

This returns a new branch (and a connection endpoint for it) containing the
data exactly as it was at that timestamp. Query it, pull out whatever's
needed, then delete the branch once you're done with it:

```bash
curl -s -X DELETE "https://console.neon.tech/api/v2/projects/$NEON_PROJECT_ID/branches/$NEW_BRANCH_ID" \
  -H "Authorization: Bearer $NEON_API_KEY"
```

## procedure B: restore a branch to a point in time (the real disaster-recovery path)

Use this when the branch itself (e.g. `production`) needs to go back in
time -- this is the actual "undo the disaster" operation.

```bash
curl -s -X POST "https://console.neon.tech/api/v2/projects/$NEON_PROJECT_ID/branches/$BRANCH_ID/restore" \
  -H "Authorization: Bearer $NEON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source_branch_id": "'"$BRANCH_ID"'",
    "source_timestamp": "2026-08-27T12:20:36.355009+00:00",
    "preserve_under_name": "pre-restore-disaster-state"
  }'
```

Important behavior confirmed during the drill: **Neon does not just discard
the pre-restore data.** `preserve_under_name` causes it to snapshot the
branch's state _before_ the restore into a new branch first, so a restore
that turns out to have used the wrong timestamp is itself recoverable. That
snapshot branch is not deleted automatically -- clean it up once you've
confirmed the restore was correct, the same way it was cleaned up at the
end of this drill.

The restore is asynchronous. Poll until it's ready before verifying:

```bash
curl -s "https://console.neon.tech/api/v2/projects/$NEON_PROJECT_ID/branches/$BRANCH_ID" \
  -H "Authorization: Bearer $NEON_API_KEY" \
  | python -c "import json,sys; print(json.load(sys.stdin)['branch']['current_state'])"
```

## verification

After either procedure, connect with the branch's connection string and run:

```bash
DATABASE_URL="postgresql://..." python scripts/verify_db_snapshot.py
```

This prints the `alembic_version`, row counts across every core table, and
the most recent `event_time` in `checkins`/`rejects`/`acs_events`. Compare
the output against what you captured _before_ the incident (or against
expectations -- e.g. "this should be back to non-zero, and the newest
`event_time` should predate the incident").

After a full branch restore, also verify:

1. `alembic current` matches `alembic heads`.
2. The application can reconnect successfully.
3. The restored database accepts a write.
4. A harmless test write can be read back successfully.

A restore is not considered validated solely because historical rows are
visible again.

## what was actually tested

This procedure was run end-to-end against the `SortTEST` Neon project
(project id `autumn-king-47468028`) on 2026-08-27, not just written from
documentation:

1. Built the real schema on an empty test branch with `alembic upgrade head`.
2. Seeded a small representative dataset (an organization, a branch, 5
   checkin rows).
3. Recorded a cutoff timestamp, then deliberately deleted all of it
   (`checkins`, `branches`, `organizations`, `customers`) to simulate a bad
   `DELETE`.
4. **Procedure A**: branched from before the cutoff -- confirmed the new
   branch had all 5 checkins, the organization, and the correct
   `alembic_version` back.
5. **Procedure B**: restored the original branch itself to the same cutoff
   -- confirmed the _original_ connection string had the data back, and
   confirmed Neon auto-preserved the deleted-data state under
   `pre-restore-disaster-state` rather than discarding it.
6. Cleaned up both temporary branches afterward.

Both procedures worked exactly as documented above on the first attempt.

### production-derived recovery drill

A second recovery drill was performed on 2026-08-27 using a temporary
branch created directly from the real SortView `production` branch. The
production branch itself was never modified.

1. Created `recovery-drill-2026-08-27` from the current production branch
   with production data and schema.

2. Verified the baseline using `scripts/verify_db_snapshot.py`:

   - organizations: 1
   - branches: 1
   - memberships: 6
   - subscriptions: 1
   - app_users: 7
   - agent_tokens: 2
   - customers: 1
   - checkins: 213,853
   - rejects: 7,618
   - acs_events: 445,301
   - pipeline_status: 1
   - alembic_version: `0a315b52b59f`

3. Verified both `alembic current` and `alembic heads` reported
   `0a315b52b59f (head)`.

4. Recorded a known pre-incident point in time, then deliberately deleted
   all 213,853 rows from `checkins` on the temporary recovery branch.

5. Verified the simulated failure with `verify_db_snapshot.py`, which
   reported `checkins: 0` while the remaining production-derived data
   remained intact.

6. Used Neon's Backup & Restore interface to preview the database at the
   pre-delete timestamp and restore `recovery-drill-2026-08-27` to that
   point in time.

7. Re-ran snapshot verification after restore. All 213,853 checkins were
   recovered, the other baseline counts matched, and
   `alembic_version` remained `0a315b52b59f`.

8. Re-ran `alembic current` and `alembic heads`; both still reported
   `0a315b52b59f (head)`.

9. Performed a post-restore write test by creating a temporary table,
   inserting and reading a row, and rolling the transaction back. The
   write/read test succeeded.

Result: **PASS**. The restore procedure has now been validated against the
real production schema and a production-derived dataset without performing
any destructive operation against the production branch itself.

## what this doc does not cover

- **Production itself has not been destructively restored.** Recovery has
  been tested against a temporary branch created directly from the real
  production branch, including production schema and data. This deliberately
  avoids introducing recovery-test risk into the live production branch.
  A direct restore of `production` should only be performed during a real
  incident when recovery is actually required.
- **No off-platform backup yet.** SortView currently relies on Neon's
  built-in PITR, and the production project has a verified one-day history
  retention window. This protects against recently discovered bad writes,
  deletions, and migrations, but does not protect against incidents discovered
  after the retention window or against certain provider/account-level
  failures. A scheduled logical backup (`pg_dump`) to storage outside the
  Neon project is therefore a planned production-hardening item before
  external customer data is onboarded.
- **No automated/scheduled drill.** This was run manually, once. Re-run it
  periodically (a quarterly fire drill is a reasonable cadence) so this
  procedure doesn't silently rot the way most untested runbooks do.
