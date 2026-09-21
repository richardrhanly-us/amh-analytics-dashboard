# Production verification runbook

Two things the merged security work cannot finish from the repository alone:

1. **Legacy plaintext admin-lock passwords** may still sit in production rows written by older versions of the Admin
   Settings page. The application now stores only hashes and still *accepts* a legacy plaintext value; this runbook
   is how the remaining rows are found and converted.
2. **Streamlit browser and log redaction** are enforced by code in this repository, but whether the *hosting
   platform* honors the configuration and captures only the scrubbed output has to be checked on a live deployment.

**Nothing in this document has been run against production.** Every production step below needs explicit approval
first (see "Rules"). Everything the repository *can* prove is proved by automated tests, named where relevant.

## Rules

- Start **read-only**. No `UPDATE` runs until a reviewed dry run has been approved.
- Never print, log, screenshot or paste a password, a hash, or `settings_json`. The tools below never output one.
- `NBPL` is production. Show the exact statement and the exact target before touching it.
- Do the migration first against a **non-production copy** (a Neon branch, see 1.3) and only then production.

---

# Part 1 -- Admin-lock credentials

## 1.1 Current storage model (verified from the merged code)

| Question | Answer |
|---|---|
| Table | `organization_settings` (one row per organization: `organization_id UNIQUE`), column `settings_json JSONB NOT NULL DEFAULT '{}'`, plus `id`, `created_at`, `updated_at`. `branch_settings` has the same shape but current code never writes a `security` block there. |
| Legacy plaintext path | `settings_json -> 'security' -> 'admin_password'` (a JSON string). Written by older versions of `src/pages/1_admin_settings.py`. |
| New hash path | `settings_json -> 'security' -> 'admin_password_hash'` (a JSON string, werkzeug scheme `scrypt:...$salt$hash`, the same as `app_users`). |
| Can both exist? | Yes. `verify_admin_password` (`src/services/admin_lock_service.py`) checks the hash **first**; the legacy value is consulted only when there is no hash. A stale plaintext value next to a hash is therefore inert. |
| Who writes the hash? | Only `build_security_settings`, when an owner/admin saves Admin Settings. It also converts a legacy value into a hash of the same password on any save (blank password field = keep the current password). |
| Is removing the legacy key safe once a hash exists? | Yes: the hash already takes precedence, so deleting the plaintext key changes nothing for authentication (`test_a_stored_hash_takes_precedence_over_a_stale_legacy_value`). |
| What is *not* a credential | An empty `admin_password` (`""`) or JSON `null` -- the old default. The application treats it as "no password". |
| Odd shapes | A non-string `admin_password` (e.g. a number) is **not** a password to the application (`_text()` returns `""`), so the lock silently would not apply. The migration does not guess: it stops. |
| Where else it can live | Branch level: `branch_settings.settings_json`. Branch values override the organization's per key, so a branch-level credential is a special case that needs a human decision. |
| Not affected | `app_users` login passwords (separate table, already hashed). |

## 1.2 Read-only inventory

`scripts/admin_lock_inventory.sql` -- two queries (`detail`, `summary`), `SELECT` only. The stored values are only
*compared* inside the server; the result contains row ids, owner ids, slugs and a state, never a password, a hash or
any part of `settings_json`. Safe for the Neon SQL editor. Run one query at a time.

| State | Meaning | Action |
|---|---|---|
| `no_lock` | no password stored | none (an empty `admin_password` key is reported in `empty_legacy_key_present`; harmless) |
| `plaintext_only` | plaintext, no hash | **migrate** |
| `hash_only` | hash only | none (already safe) |
| `both` | hash **and** stale plaintext | migrate: drop the stale plaintext key |
| `unexpected_shape` | JSON not shaped as the application writes it | **stop; a person decides** |

The same classification is implemented in Python (`scripts/migrate_admin_lock_hashes.py`) and
`tests/test_admin_lock_migration_postgres.py` proves the SQL and the Python agree on every state, including the
unexpected shapes, and that the query returns no secret.

**Run it only after approval.** Target: the production Neon database, via the Neon SQL editor or any read-only
session. Decision: if the summary shows no `plaintext_only` and no `both` rows in `organization_settings`, and no
non-`no_lock` rows in `branch_settings`, Part 1 is finished and no migration is needed.

## 1.3 Migration procedure

The tool is `scripts/migrate_admin_lock_hashes.py`. It is the safest way because the hash must be computed in Python
by the application's own helper (`hash_admin_password`) -- SQL cannot produce a matching scrypt hash, and reversible
SQL-side encryption is deliberately not used -- and because it can prove its own result before committing.

**Behavior (all covered by tests against a real PostgreSQL):**

- **Dry run by default**, in a `READ ONLY` transaction (it cannot write). It prints only counts, settings row ids,
  organization ids and slugs, and state names. `DATABASE_URL` comes from the environment, never the command line.
- **Fail closed:** any `unexpected_shape` row, or any admin-lock key at branch level, stops the run (exit 2) with no
  change, and blocks `--execute` for *every* row until resolved.
- **Execute needs all three:** `--execute`, `--confirm-database <name>` equal to the actual target database, and
  explicit `--row-id` values copied from the reviewed dry run.
- **One transaction.** It locks the approved rows (`SELECT ... FOR UPDATE`, so nothing can change between check and
  update), re-classifies them under the lock, hashes the plaintext in memory, and runs a guarded `UPDATE`
  (`jsonb_set` / `#-`, so every unrelated setting is untouched). The `WHERE` clause re-tests the row id and the old
  state **on the server**, so the plaintext is never sent back to the database. Exactly one row must match; the row is
  then re-read and its whole JSON compared with the expected result; the stored hash must verify the original
  password. Any failure rolls the whole transaction back.
- `plaintext_only` -> hash stored, plaintext key removed. `both` -> only the stale plaintext key removed.
  `hash_only` / `no_lock` -> skipped. **Re-running is a no-op.**
- Nothing sensitive is printed or logged; SQLAlchemy parameters are hidden; a failure prints only the exception
  type and location.

**Procedure (each production step needs approval):**

1. **Rehearse on a copy.** Create a Neon branch of production (or restore a snapshot to a throwaway database). Treat it
   as sensitive -- it contains the same plaintext -- and delete it afterwards. Point `DATABASE_URL` at the copy.
2. `python scripts/migrate_admin_lock_hashes.py` -- review the counts and ids.
3. `python scripts/migrate_admin_lock_hashes.py --execute --confirm-database <name> --row-id <id> ...` on the copy.
4. Re-run the dry run: it must report `plaintext_only 0, both 0`. Run the inventory SQL: same result.
5. Have an owner/admin of a migrated organization confirm, on a deployment pointed at the copy, that their **existing**
   password unlocks Admin Settings and a wrong one does not.
6. Repeat steps 2-4 on production with the approved ids. Record the Neon point-in-time timestamp immediately before
   (see rollback).
7. After production: dry run reports nothing left; inventory shows no `plaintext_only` / `both`; an org admin confirms
   unlock with the existing password and rejection of a wrong one.

## 1.4 Rollback

- **The plaintext is never restored, by design.** The script keeps no copy of it. A "rollback" that put plaintext back
  would re-create the exposure this work removes.
- **Roll code forward, not back.** The merged application understands both formats. If application code *must* be
  rolled back to a version from before the hash was introduced, that old code reads only `admin_password`, so a
  migrated organization would have **no lock** (the Admin Settings prompt disappears; owner/admin role checks still
  apply) until the new code is redeployed. Do not set a new password in the old code -- it would store plaintext
  again.
- **A wrong hash (locked-out admin).** The script verifies the hash before and after writing, so this is not
  expected. Break-glass, with approval, for one row: remove the hash so the lock is off, then have the admin set a new
  password in the application:
  `UPDATE organization_settings SET settings_json = settings_json #- '{security,admin_password_hash}', updated_at = CURRENT_TIMESTAMP WHERE id = <row id>`.
- **Point-in-time restore is a last resort.** It restores the whole database (losing everything written since) *and*
  brings the plaintext back.

## 1.5 Limits -- what migrating the rows does not remove

- **Neon history.** Earlier states of the rows stay in Neon's point-in-time/branch history for the plan's retention
  window. The plaintext ages out with it; it cannot be purged from the repository side. (Retention setting: check the Neon console.)
- **Git history.** `src/branch_settings.json` carried a password value in earlier commits (removed from the file since).
  If that value was or is a real admin password, rotate it: after the migration an owner/admin sets a new password in
  Admin Settings.
- Copies made by anyone who read the database or an old backup before the migration are outside our control.

---

# Part 2 -- Live Streamlit redaction verification

## 2.1 What is guaranteed where

| | Proved by | Where |
|---|---|---|
| The setting `client.showErrorDetails = "none"` is valid and read from `.streamlit/config.toml` when Streamlit starts in the repo root | `tests/test_streamlit_error_details.py` | repo |
| An uncaught exception shows the browser only the generic message (no message, type, traceback, secret) | `tests/test_streamlit_error_details.py`, `tests/test_real_apps_redaction.py` | repo |
| Streamlit's own log of that exception carries no message/values, only type, SQLSTATE and SortView's frames; the `rich` console print is off | `tests/test_streamlit_log_scrubbing.py`, `tests/test_real_apps_redaction.py` (real main app + real Super Admin app, real DB failure) | repo |
| Every Streamlit entry script installs the scrubber before anything else | guard in `tests/test_streamlit_log_scrubbing.py` | repo |
| The same on Streamlit **1.63.0** (the pinned production version) without `rich` | those suites re-run against the pinned dependency set | repo |
| The hosting platform starts the app from the repository root (so the file is found) | **cannot be proved from the repo** | **hosting** |
| The platform does not override `client.showErrorDetails` (an environment variable or `--client.showErrorDetails` flag outranks the file) | **cannot be proved from the repo** | **hosting** |
| The platform captures only the process's stdout/stderr, and its log viewer shows the scrubbed line | **cannot be proved from the repo** | **hosting** |
| The Super Admin app's launch command and working directory | not documented anywhere in the repo | **hosting** |
| Import-time failures on a cold start, before the scrubber runs; text from other libraries' loggers or `print`; the platform's own infrastructure logs | not covered by the scrubber | residual |

## 2.2 Set up an isolated check (never in the production apps)

Use **separate, non-production** Streamlit apps from the same repository and branch (or a branch containing the
release under test). Do not change the production apps' secrets or settings. Delete these apps when done.

**A. The canary app** -- exercises every canary and the callback path.

- Main file: `scripts/redaction_canary/app.py`.
- Secret/environment variable: `SORTVIEW_REDACTION_CANARY_ENABLED = "true"`. Without it the app is inert.
- It shows an **Effective configuration** panel and three buttons that each raise an uncaught exception whose message
  contains: a fake password, database URL, API token, patron/card number and e-mail, a fake SQL statement and bound
  value, and `Failing row contains (...)`. All values start with `CANARY-` / `canary-` / `canary_`.

**B. The two real apps** -- prove each real entry script, with a real failure and no code changes.

- One app with main file `src/app.py`, one with `super_admin/Super_Admin_Home.py`.
- Secret `DATABASE_URL = "postgresql://canary_svc_user:CANARY-DB-PASSWORD-LIVE-0001@canary-db-host-live.example.invalid:5432/canary_db?connect_timeout=3"`
  -- a database that does not exist. Leave every other secret as the app requires to start.
- Submit the login form with fake credentials (`nobody@example.invalid` / any password). `authenticate_user` queries
  the database with nothing catching the failure, so this is a genuine uncaught exception. (Only the *host* canary can
  appear in a connection error message; the other canary classes are exercised by app A.)

## 2.3 Checklist

Record the result of each line. **Any FAIL means redaction is not effective on that platform.**

**On the canary app (A):**

1. Open the app. The **Effective configuration** panel must show
   - `client.showErrorDetails` = `none`, and `client.showErrorDetails defined in` = a path ending
     `.streamlit/config.toml`;
   - `repository_config_file_in_working_directory` = `true`;
   - `log_scrubber_installed` = `true`, `logger.enableRich` = `false`.
   - FAIL if `defined in` says `command-line argument or environment variable` (the platform overrides it),
     or `<default>` / value `full` (the file is not being found -- wrong working directory).
2. Click each of the three buttons in turn, noting the time of each click. For **each**, the browser must show only
   *"This app has encountered an error. The original error message is redacted to prevent data leaks..."* --
   **no** exception message, **no** exception type (`RuntimeError`, `DataError`), **no** traceback, and none of the
   canaries (password, token, database URL, card number, e-mail, SQL, bound value). Also check the browser's developer
   tools network/WebSocket messages if you can: the same must hold.
3. Open the platform's log view (Streamlit Community Cloud: the app's **Manage app** panel, per Streamlit's own message).
   For each click there must be one line of the form
   `Uncaught app execution | error_type=<type> [sqlstate=22P02] [cause_type=...] at=... app=...app.py:<line>:raise_...`
   -- type and location only.
4. **Search the log** (the viewer's search, or export and `grep`) for each of: `CANARY-`, `canary-`, `canary_`,
   `postgresql://`, `Failing row`, `Bearer`, `Traceback (most recent call last)`, `example.invalid`. **Every search must
   return nothing.** Any hit = the exception text leaked into the hosting log.

**On the real apps (B), one at a time:**

5. Open the app, submit the fake login. Browser: the same generic message only (no type, no traceback).
6. Log: one `Uncaught app execution | error_type=sqlalchemy.exc.OperationalError cause_type=psycopg2.OperationalError ...`
   line whose `app=` names `get_user_by_email < authenticate_user <` and `app.py` (main app) or `super_auth.py`
   (Super Admin). Search for `canary-db-host-live`, `canary_svc_user`, `CANARY-DB-PASSWORD`, `canary_db`,
   `postgresql://`: **no hits.**
7. This proves the repository config **and** the scrubber reached each real app on this platform. If either real app
   fails while the canary app passes, that app is being started differently (working directory or entry script).

**Also confirm:**

8. The platform's settings/secrets for these apps set no `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS` /
   `--client.showErrorDetails`. (Check 1 detects an override; this is the manual cross-check.)
9. Note the Python and Streamlit versions the platform reports; they should match `requirements.txt`
   (`streamlit==1.63.0`; Python 3.11 -- see "python version" in `docs/deployment.md`).

## 2.4 Clean up

Delete the canary app and the two check apps, and any secrets you added. Search the hosting log once more for `CANARY-`
-- the log lines from the check are safe by design, but confirm nothing else recorded a canary (for example the
platform's own request log).

## 2.5 What this check cannot tell you

- Exceptions raised **before** the scrubber is installed on a process's first script run (an import failure at
  `app.py`'s import lines) are logged by Streamlit unscrubbed. They contain module names and file paths, not data, but
  they are not covered.
- Anything the platform logs about the app *outside* the process's stdout/stderr, or retains after you delete the app.
- Streamlit fragments (`st.fragment`) were not exercised; callbacks were.
- A different Streamlit version than the one pinned: the tests cover 1.52.0 and 1.63.0. After any Streamlit upgrade,
  re-run `tests/test_streamlit_log_scrubbing.py`, `tests/test_real_apps_redaction.py` and this checklist.
