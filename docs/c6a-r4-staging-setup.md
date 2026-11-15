# C6a R4 — staging environment for stage 1

**Status:** proposed procedure, reviewed against the code on 2026-11-13. **Nothing has been provisioned.** Option A (section 1, item 1) was approved and implemented on 2026-11-13. The cost approval and the Neon account check (items 2 and 3) remain before provisioning.

**For:** the operator setting up and later removing the throwaway staging environment. That operator holds the DigitalOcean and Neon accounts and runs everything here from the host.

**What it is:**
- a new, empty Neon database (never a branch of production);
- a temporary DigitalOcean app running the backend at `main` (`46ac69a`);
- the unchanged R3 admin app, run locally;
- Windows VM clones running the unchanged Setup 1.0.14 console engine with `-ApiUrl`.

Replacement is turned on in staging only, after the isolation checks pass. **Production's flag stays off, and production is never written to.**

## 1. Before provisioning: what the code supports, and what's needed

| Need | Unchanged code? | Finding |
|---|---|---|
| A fresh database | **Yes** | The baseline migration rebuilds the full schema, so `alembic upgrade head` alone builds a fresh database (`docs/database-migrations.md`). The PostgreSQL tests already build every test database this way, with the runtime role created afterwards. Plans are seeded by the baseline. |
| The runtime role and its grants | **Yes** | `scripts/runtime_role_privileges.py provisioning-sql` prints the exact production grants, and `verify` checks them. Migrations that grant to `sortview_app` skip the grant if the role doesn't exist yet. |
| The backend | **Yes** | It runs from environment variables only. Contract 2 uploads need `SORTVIEW_V2_INGEST_ENABLED=true`. |
| Setup with a staging address | **Yes** | The console engine accepts `-ApiUrl` if it is `https://<host>[/path]`. Its connection check is a `GET` of `<ApiUrl>/`, and the backend answers `200` there. On resume it reuses the address saved with the enrollment, and refuses a different `-ApiUrl`. |
| The first platform administrator | **Yes, by hand** | No code path sets `app_users.is_platform_admin`, so production administrators were evidently granted directly in the database. Staging does the same, once, on the staging database only (section 4). No application change is needed. |
| The admin app against staging | **Yes, after option A** | A development-only proxy setting, `DEV_API_PROXY_CHANGE_ORIGIN` (item 1 below). |

### Item 1 (resolved): the admin app's development proxy and a DigitalOcean app

**Resolved 2026-11-13 by option A:**
- **The setting:** `frontend/devApiProxy.ts`, used by `frontend/vite.admin.config.ts`, reads a new development variable, `DEV_API_PROXY_CHANGE_ORIGIN`.
  - Exactly `true` (after trimming) sets `changeOrigin: true`.
  - Unset, or any other value (including `TRUE` or `1`), keeps the previous behavior.
  - The customer app's `vite.config.ts` is unchanged.
- **Tests:** `frontend/devApiProxy.test.ts`.
- **Checked against a running development server:**
  - unset and `TRUE` forwarded the browser's `Host: localhost:5174`;
  - `true` forwarded the target's host;
  - the browser's `Origin: http://localhost:5174` reached the backend unchanged in every case.
- **The production build is unaffected:** an admin build made with a made-up staging target and the variable set contained neither the target nor the variable's name.

The analysis that led to it:

`frontend/vite.admin.config.ts` proxies `/api` with `changeOrigin: false`, so the backend receives `Host: localhost:5174`. DigitalOcean's edge routes by host name and refuses a request for an unknown host.

Checked read-only on 2026-11-13 with a `GET` of the production backend's `/`:
- the correct host returned `200`;
- `Host: localhost:5174` returned `403`.

A staging app on DigitalOcean would behave the same way. **Choose one:**

- **A. A narrow, development-only change (approved and implemented).** `changeOrigin` becomes `true` only when the development variable `DEV_API_PROXY_CHANGE_ORIGIN=true` is set.
  - `vite build` ignores the `server` block entirely, so the production admin build and its deployment are unchanged.
  - The browser's `Origin` header still reaches the backend unchanged (`http://localhost:5174`), so the backend's origin checks apply exactly as they do in production.
  - The session cookie is set for `localhost`. Chrome and Edge accept a `Secure`, `__Host-` cookie on `http://localhost`. Check this in step 5.4, and see the fallback there.
- **B. No change: use the admin API from PowerShell.** Sign in and send each admin request with `Invoke-WebRequest`, adding `Origin: http://localhost:5174`.
  - This works with unchanged code, but it is clumsy and easy to get wrong.
  - It puts the one-time enrollment codes in console output.
  - It doesn't exercise the R3 interface.
- **C. A separate local reverse proxy** (Caddy, nginx). This adds a tool and its configuration for no gain over A.

Everything else in stage 1 uses the application unchanged.

### Other items to settle before provisioning

2. **Approval of the cost** (section 7).
3. **Neon:** confirm the account can create a separate project, on its plan or as a paid project. Use a new project, never a branch. A branch would copy production data.
4. **Build settings:** read the production component's build and run settings with `doctl apps spec get <production app id>`. That command is read-only; don't paste its output anywhere. Staging copies those settings, never the values of its secrets.
   - The run command recorded in `docs/react-hosting-runbook.md` is `uvicorn main:app --host 0.0.0.0 --port 8080`.
   - The repository has no pinned Python version for the backend, so record the version in staging's build log and compare it with production's.

## 2. Secrets and configuration (names only, never values)

**New secrets, created for staging, used nowhere else and destroyed at cleanup:**

| Secret | Where it lives | Used by |
|---|---|---|
| Neon owner connection string (`STAGING_OWNER_URL`) | the operator's session only | migrations, the role grants, the administrator bootstrap, the read-only checks |
| `sortview_app` password, and its connection string | DigitalOcean, as the encrypted `DATABASE_URL` | the staging backend |
| Staging administrator password | the operator's password manager | signing in to the staging admin app |
| Enrollment codes and agent tokens | shown once, never recorded | Setup on the clones |

Never reuse a production password, connection string or token. Never put a staging secret in a file in the repository, a ticket, chat, or a screenshot.

**Staging backend environment:**

| Variable | Staging value | Why |
|---|---|---|
| `DATABASE_URL` | **secret:** `sortview_app` on the staging database | the runtime role, under row-level security, as in production |
| `SORTVIEW_V2_INGEST_ENABLED` | `true` | Contract 2 uploads and status calls |
| `SORTVIEW_MACHINE_REPLACEMENT_ENABLED` | `false` at first; `true` only after section 6 passes | the gate under test |
| `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` | `http://localhost:5174` | the admin app signs in through `/api/auth/login` |
| `SORTVIEW_ADMIN_ALLOWED_ORIGINS` | `http://localhost:5174` | admin writes |
| `SORTVIEW_LIVE_TIMEZONE` | `America/Chicago` | as in production |
| `SORTVIEW_API_DOCS_ENABLED` | unset (`false`) | as in production |
| `SORTVIEW_CUSTOMER_COOKIE_SECURE` | unset (`true`) | as in production; the fallback is in step 5.4 |

**Leave these unset:**
- `SENTRY_DSN`, so staging errors don't go to production's Sentry project;
- every `SORTVIEW_SMTP_*` and `SORTVIEW_EMAIL_FROM`, so staging can't send email;
- `SORTVIEW_CUSTOMER_APP_URL`.

Compare the list of names, never the values, with production's spec. Add nothing else unless the backend fails without it, and record any addition.

## 3. The staging database

1. **Create the project.** In Neon, create a **new project**, for example `sortview-r4-staging`, in the same region as production, with a database named `sortview_r4_staging`. Don't create it as a branch.
2. **Load the owner connection string into the session, never onto the command line:**

   ```powershell
   $s = Read-Host -AsSecureString "Staging OWNER connection string"
   $env:STAGING_OWNER_URL = [Runtime.InteropServices.Marshal]::PtrToStringAuth([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
   Remove-Variable s
   ```

3. **Prove it's the new, empty staging database.**

   ```powershell
   .venv\Scripts\python -c "import os; from sqlalchemy.engine import make_url; u = make_url(os.environ['STAGING_OWNER_URL']); print(u.host, u.database)"
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" $env:STAGING_OWNER_URL -c "SELECT current_database(), (SELECT count(*) FROM pg_tables WHERE schemaname = 'public') AS tables"
   ```

   **Pass:**
   - the host is the new project's endpoint, not production's (compare the first label, `ep-…`, with production's);
   - the database is `sortview_r4_staging`;
   - `tables` is `0`.

   Stop if any of these differ.

   **Identify the project and endpoint.** From the Neon console, note:
   - the **new** project's **Project ID**;
   - its compute **Endpoint ID** (`ep-…`);
   - the **production** project's Endpoint ID. Read it from the console page only; nothing here connects to production.

   Then check that `STAGING_OWNER_URL` really points at that staging endpoint and not at production's. This is the **endpoint check**; section 4 runs it again.

   ```powershell
   $stagingProject     = Read-Host "Staging Project ID (the NEW project)"
   $stagingEndpoint    = Read-Host "Staging Endpoint ID (ep-..., the NEW project's compute)"
   $productionEndpoint = Read-Host "Production Endpoint ID (ep-..., read from the console only)"
   $urlEndpoint = .venv\Scripts\python -c "import os; from sqlalchemy.engine import make_url; print(make_url(os.environ['STAGING_OWNER_URL']).host.split('.')[0].removesuffix('-pooler'))"
   if ($stagingEndpoint -notmatch '^ep-[a-z0-9-]+$' -or $productionEndpoint -notmatch '^ep-[a-z0-9-]+$') { throw "An Endpoint ID is not in the ep-... form." }
   if ($stagingEndpoint -eq $productionEndpoint) { throw "Refused: the staging and production Endpoint IDs are the same." }
   if ($urlEndpoint -eq $productionEndpoint) { throw "Refused: STAGING_OWNER_URL points at PRODUCTION." }
   if ($urlEndpoint -ne $stagingEndpoint) { throw "Refused: STAGING_OWNER_URL points at $urlEndpoint, not the staging endpoint." }
   $marker = "sortview-r4-staging project=$stagingProject endpoint=$stagingEndpoint"
   ```

   **Mark the database as staging.** Only after the endpoint check passes. The marker is the server-side identity that section 4 checks:

   ```powershell
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" $env:STAGING_OWNER_URL -c "COMMENT ON DATABASE sortview_r4_staging IS '$marker'"
   ```

   Only this database is ever marked. Production is never connected to anywhere in this procedure.

4. **Migrate as the owner:**

   ```powershell
   $env:DATABASE_URL = $env:STAGING_OWNER_URL
   .venv\Scripts\python -m alembic upgrade head
   .venv\Scripts\python -m alembic current
   Remove-Item Env:DATABASE_URL
   ```

   **Pass:** `alembic current` shows the repository's head.

5. **Create the runtime role with exactly production's privileges.** Use the same statements as `tests/test_admin_installations_postgres.py`.
   - Generate the grants for review:

     ```powershell
     .venv\Scripts\python scripts\runtime_role_privileges.py provisioning-sql > $env:TEMP\r4-grants.sql
     ```

   - Then, as the owner:
     - `CREATE ROLE sortview_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT PASSWORD <new>`, entering the password at a psql prompt (`\password sortview_app`), never on the command line;
     - `GRANT CONNECT ON DATABASE sortview_r4_staging TO sortview_app`;
     - apply `r4-grants.sql`, after reviewing it.
   - Delete `r4-grants.sql` afterwards.
   - Check the grants:

     ```powershell
     $env:DATABASE_URL = $env:STAGING_OWNER_URL
     .venv\Scripts\python scripts\runtime_role_privileges.py verify
     Remove-Item Env:DATABASE_URL
     ```

   **Pass:** `verify` exits `0`.

## 4. The first staging administrator (staging only)

The insert is the only privileged write in this procedure. It runs only if **every** one of these independent checks passes:

| # | Check | Where | Guards against |
|---|---|---|---|
| 1 | The connection string's endpoint is the staging Endpoint ID entered from the Neon console, and differs from production's | on the host, before connecting (the endpoint check, step 3.3) | a production or wrong connection string in `STAGING_OWNER_URL` |
| 2 | The database carries the marker `sortview-r4-staging project=<Project ID> endpoint=<Endpoint ID>` for that same project and endpoint | in the database, inside the insert's transaction | the wrong database behind the right-looking host; a marker from a different staging project |
| 3 | The database is named `sortview_r4_staging` | same transaction | an extra safeguard |
| 4 | It has no user and no library | same transaction | an extra safeguard: a database that is already in use |

Checks 3 and 4 are extra safeguards, never the only ones. The checks and the insert run in one transaction: a refusal ends the session, the transaction rolls back, and nothing is written. The insert writes one row and touches nothing else. Application authentication is unchanged.

1. **Run the endpoint check again** (step 3.3), in the same PowerShell session, so `$marker` holds this project's values. Stop if it throws.
2. **Hash the password locally**, using the same function the application uses (`werkzeug.security.generate_password_hash`, as in `auth_service.create_user`). The password is typed at a hidden prompt and only the hash is kept:

   ```powershell
   $hash = .venv\Scripts\python -c "import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass.getpass('New staging admin password: ')))"
   ```

   - Use a new, long password from the password manager.
   - Direct insertion skips the application's password-policy check, so choose a password that would pass it.
3. **Save this guarded insert** as `$env:TEMP\r4-admin.sql`:

   ```sql
   \set ON_ERROR_STOP on
   BEGIN;
   SELECT coalesce(shobj_description(d.oid, 'pg_database'), '') = :'marker' AS is_marked,
          current_database() = 'sortview_r4_staging' AS is_staging_name,
          (SELECT count(*) FROM app_users) = 0 AND (SELECT count(*) FROM organizations) = 0 AS is_empty
   FROM pg_database d
   WHERE d.datname = current_database()
   \gset
   \if :is_marked
   \else
     \echo 'Refused: this database does not carry the staging marker for that project and endpoint.'
     \quit
   \endif
   \if :is_staging_name
   \else
     \echo 'Refused: not the database named sortview_r4_staging.'
     \quit
   \endif
   \if :is_empty
   \else
     \echo 'Refused: this database already has users or libraries.'
     \quit
   \endif
   INSERT INTO app_users (email, full_name, password_hash, is_active, is_platform_admin)
   VALUES (lower(:'email'), 'R4 staging administrator', :'hash', TRUE, TRUE)
   RETURNING id, email, is_active, is_platform_admin;
   COMMIT;
   ```

4. **See it refuse first.** Run the command below once with `-v "marker=wrong"` in place of `-v "marker=$marker"`.

   **Pass:** it prints "Refused: this database does not carry the staging marker…", and `SELECT count(*) FROM app_users` still returns `0`.

   **Then run it for real as the owner, and clear the hash:**

   ```powershell
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" $env:STAGING_OWNER_URL -v email=<staging admin email> -v "hash=$hash" -v "marker=$marker" -f $env:TEMP\r4-admin.sql
   Remove-Variable hash; Remove-Item $env:TEMP\r4-admin.sql
   ```

   **Pass:** one row, with `is_platform_admin` `t`. Use an address that isn't a production account's, for example a `+r4staging` alias. Staging sends no email.

Production authentication isn't touched: no code changes, and nothing runs against production.

## 5. The staging backend

1. **Create the app.** Create a new DigitalOcean app, `sortview-r4-staging`, from this repository's `main` branch.
   - Turn **automatic deploys on push off**, so later merges don't redeploy it.
   - One web service, with the production component's build and run settings (section 1, item 4) on the smallest instance size.
   - HTTP port `8080`, health check path `/`.
   - No custom domain, no jobs, no database component.
   - Set the variables in section 2, with **the replacement flag `false`**.
2. **Deploy once.** Record the deployed commit (expect `46ac69a`) and the Python version from the build log.
3. **Check it answers:**

   ```powershell
   $staging = "https://<staging app>.ondigitalocean.app"
   (Invoke-WebRequest "$staging/" -UseBasicParsing).StatusCode
   ```

   **Pass:** `200`, with no certificate warning.
4. **Admin app** (option A). Create `frontend\.env.development.local`, which Git ignores (`.env*.local`) and is never committed:

   ```
   DEV_API_PROXY_TARGET=https://<staging app>.ondigitalocean.app
   DEV_API_PROXY_CHANGE_ORIGIN=true
   ```

   - `DEV_API_PROXY_CHANGE_ORIGIN` must be exactly `true`. Without it, DigitalOcean answers `403`.
   - Neither variable is `VITE_`-prefixed, so neither ever reaches a built bundle.

   Start the development server and open the app:

   ```powershell
   cd frontend
   npm run dev:admin
   ```

   Open `http://localhost:5174` and sign in as the staging administrator. Delete `.env.development.local` when stage 1 ends (section 8), so later local development doesn't point at a deleted app.

   **Pass:**
   - sign-in succeeds, and the session cookie is set for `localhost`;
   - **Libraries** is empty.

   If the browser refuses the `Secure` cookie on `http://localhost`, set `SORTVIEW_CUSTOMER_COOKIE_SECURE=false` **on staging only**. That falls back to a non-`__Host-` cookie name, and production is unaffected. Record that you did it.
5. **Seed, through the admin app:**
   - library `sortview-r4-staging`, with its primary branch;
   - its operational identity;
   - Collector settings: time zone `America/Chicago`, and synthetic classification lists using the CANARY names from `tests/collector_v2_support.py`;
   - internal routing, if the page requires it.

## 6. Isolation checks (all must pass before the flag is turned on)

| Check | How | Pass |
|---|---|---|
| Separate database | step 3.3 | a different endpoint, `sortview_r4_staging` |
| No production data | `SELECT slug FROM organizations` as the staging owner | only `sortview-r4-staging` |
| Runtime role | `runtime_role_privileges.py verify` on staging | exit `0` |
| Staging backend uses staging | the staging admin app's **Libraries** | only `sortview-r4-staging`; `sortview-g2-qualification-2` is absent |
| Production untouched | production admin, `sortview-g2-qualification-2` → Installations | the flag-off notice still shows; installation #9 is still Active and seen recently |
| Production flag | production's installation list | `replacement_enabled=false` |
| No shared secrets | compare names in the two app specs; values were created new | no staging value equals a production one |
| No email, no Sentry | staging environment | the SMTP variables and `SENTRY_DSN` unset |

Then set `SORTVIEW_MACHINE_REPLACEMENT_ENABLED=true` **on the staging app only**, which redeploys it.

**Pass:** the staging Installations page has no flag-off notice, while production's still has it.

## 7. Cost (estimates)

All amounts are **estimates**. Check them against the current price pages before approving.

| Item | Estimate | Note |
|---|---|---|
| DigitalOcean app, one smallest web service | about $5–12 a month, prorated | billed by time; one to two weeks is likely a few dollars |
| Neon project | $0 if the plan has a free project slot; otherwise usage-based, likely a few dollars for a small, mostly idle database | depends on the account's plan and project limit |
| Bandwidth and build minutes | negligible | small test traffic |
| VMware clones | none | local disk: about 20–40 GB each for full clones (estimate) |

**Total: likely under about $15 for a two-week stage 1 (estimate).**

## 8. Cleanup

After stage 1's evidence is recorded in `docs/c6a-r4-qualification.md`:

1. Disable the `SortView Collector` task on each clone (or just shut them down).
2. **DigitalOcean:**
   - delete the app with `doctl apps delete <staging app id>`, which removes its environment and secrets;
   - confirm `https://<staging app>.ondigitalocean.app/` no longer answers;
   - confirm with `doctl apps list` that the **production** app is still listed and healthy.
3. **Neon:** delete the **project** `sortview-r4-staging`, which removes its database, roles and data. Confirm the production project is untouched.
4. **Host:** delete `frontend\.env.development.local`, then run `Remove-Item Env:STAGING_OWNER_URL`, and delete any temporary SQL files.
5. **VMware:** delete the clones from disk, after keeping any logs needed as evidence (with no secrets in them). VM-A and the clean snapshot are not touched.
6. **Production check:** the production admin page still shows the flag-off notice, installation #9 is Active, and optionally `stage0_baseline.sql` still returns the same counts and fingerprints.
7. Record the deletion dates in `docs/c6a-r4-qualification.md`.
