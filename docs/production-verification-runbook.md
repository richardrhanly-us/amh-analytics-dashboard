# Production verification runbook

How SortView's production is verified, and the record of each production verification so far:

- **Routine production check** (below): what to confirm after a deployment or a production change.
- **Part 3, the production React admin app** (`https://admin.sortviewapp.com`, A3). Recorded 2026-10-15.
- **Part 4, the production React customer app** (`https://app.sortviewapp.com`, A4). Recorded 2026-10-16.
- **Part 5, the retirement of the customer Streamlit app** (A5). Recorded 2026-10-17.

Parts 1 and 2 (the legacy admin-lock password migration and the hosted Streamlit redaction checks) were removed when
A5 retired the customer Streamlit app, the admin lock and their tooling; they are in git history. The numbering of
Parts 3 to 5 is kept so that existing references stay valid.

## Rules

- Start **read-only**. No production write runs until its exact statement and target have been reviewed and approved.
- Never print, log, screenshot or paste a password, a hash, a token, a session cookie or raw `settings_json`.
- `NBPL` is production. Show the exact statement and the exact target before touching it.
- Rehearse a data change on a **non-production copy** (a Neon branch) before production.

## Routine production check

After a backend, customer or admin deployment, or a production configuration change, confirm at least:

| Area | Check |
|---|---|
| Backend | `GET https://sortview-app-2p336.ondigitalocean.app/` answers `{"status": "SortView API running"}`; the deployed commit is the intended one |
| Customer app | `https://app.sortviewapp.com` loads; a deep route (a sorter's reports) loads directly and on refresh |
| Customer API through CloudFront | `GET https://app.sortviewapp.com/api/auth/session`, signed out, answers 401 JSON (never the app's HTML) |
| Customer sign-in and data | Sign in as an NBPL user; Live Today and Reports show current data; sign out |
| Customer writes | A settings save (or sign-in/sign-out, which are guarded writes) succeeds, so the customer Origin guard accepts the app |
| Admin app | `https://admin.sortviewapp.com` loads; a platform administrator can sign in, open a library and sign out; `/api/admin/session` answers 401 JSON when signed out |
| Collectors and pipeline | The latest scheduled "Pipeline health check" run is green, and Live Today's "last reported" keeps advancing |
| Allowed origins | `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` still contains `https://app.sortviewapp.com`, `https://admin.sortviewapp.com` and `https://d22i2vjcxygt0c.cloudfront.net`; `SORTVIEW_ADMIN_ALLOWED_ORIGINS` contains `https://admin.sortviewapp.com` (hosting runbook, M.5 and L.6) |

The hosting runbook's sections L.7 and M.7 list the per-app checks after each frontend deployment, and its section I
the client-IP check to repeat if the `/api/*` path or the backend's hosting changes.

---

# Part 3 -- Production React admin app verification

The hosting itself is described in [`react-hosting-runbook.md`](react-hosting-runbook.md), section L. This part
records the production smoke test of A2 items 1-7 through that hosting. Recorded 2026-10-15.

No credential, session cookie, token or enrollment code is recorded here, and none may be added.

## 3.1 What was under test

| Item | Value |
|---|---|
| Admin app | `https://admin.sortviewapp.com`: its own CloudFront distribution and private, versioned S3 bucket |
| Frontend artifact | `npm ci` + `npm run build:admin` → `frontend/dist-admin`; uploaded and invalidated as in the hosting runbook, L.5 |
| Backend | DigitalOcean `sortview-app-2p336.ondigitalocean.app`, source `main` at `64c7d7b` (PR #210, A2 item 7). The environment-variable redeploy kept that commit; the app was healthy. |
| Allowed origins | `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` = the customer trial origin + `https://admin.sortviewapp.com`; `SORTVIEW_ADMIN_ALLOWED_ORIGINS` = `https://admin.sortviewapp.com`; `SORTVIEW_CUSTOMER_APP_URL` unchanged |
| Database schema | The deployed code's Alembic head is `f7b3d2c81a46` (`alembic heads`). **`alembic current` could not be read**, because the application's runtime role is refused `alembic_version` (`permission denied for table alembic_version`). That refusal is by design (`scripts/runtime_role_privileges.py`: no access to `alembic_version`), so the grant was not changed. Every A2 admin flow below ran successfully in production, which proves functionally that the schema they need is present. It is not a direct reading of the revision. |

## 3.2 Results

**Sign-in and session**

| Check | Result |
|---|---|
| Platform administrator signs in at `admin.sortviewapp.com` | PASS |
| Session survives navigation; `/libraries` loads authenticated admin data | PASS |
| Sign out, then sign in again | PASS |
| A normal customer account signs in | PASS: refused with "No access" / "This account does not have SortView administration access." |

**Origin guard**

| Check | Result |
|---|---|
| `POST` through the admin distribution's `/api/*` with `Origin: https://evil.example` | PASS: HTTP 403, `{"code":"origin_not_allowed","message":"Request origin is not allowed."}` |

**Reads**

All PASS:
- libraries list;
- library overview;
- Collector configuration;
- Internal Routing;
- sorter list;
- sorter diagnostics, including Contract v2 heartbeat, cutover and health data.

**Writes on an existing test library ("Rehearsal Library")**

| Check | Result |
|---|---|
| Save Collector settings | PASS: the routing-review marker updated, and "Last reviewed" moved to the time of the save |
| Rename installation "Main AMH Sorter" to a temporary name, then back | PASS: both saves succeeded, and the name is "Main AMH Sorter" again |

**Provisioning and the disposable library**

| Check | Result |
|---|---|
| Provision "SortView Admin Smoke Test" (`sortview-admin-smoke-test`), primary branch "Main Branch" (`main`), plan Starter | PASS: active library, Starter trial subscription, active primary branch, operational identity Assigned. No owner was created (first owner is C3). |
| Prepare it for Contract 2 and add installation #6, "Smoke Test Sorter" | PASS: Contract 2, status Provisioning |
| Issue an enrollment code | PASS: issued and shown once. It was never used, and its value is not recorded. |
| Suspend, then reactivate | PASS: Active → Suspended with the correct notice, then Suspended → Active |
| Offboard permanently | PASS: Active → Cancelled. 0 agent tokens deactivated, 1 installation retired, 1 unused enrollment code revoked, 0 ingest keys retired, 0 sessions revoked. |

**Customer and Collector regression**

| Check | Result |
|---|---|
| NBPL customer UI (Streamlit) after the admin deployment and the backend redeploy | PASS: loads normally, with the LIVE indicator, Pipeline OK, today's metrics, a recent pipeline report and working sign-in |
| Production collectors | PASS: Contract v2 and status calls continued to appear in the DigitalOcean runtime logs |

No customer was moved, and the customer React trial is separate from the admin app.

## 3.3 Client IP observation

- **What was seen:** the DigitalOcean runtime logs showed the admin session's requests consistently from one
  client/source address, while unrelated requests came from other addresses. The forged-Origin request (02:40:43 UTC)
  came from the same address as the rest of that session.
- **What it suggests:** the behavior is consistent with the viewer's IP being preserved through CloudFront, rather
  than every request collapsing to one CloudFront edge address.
- **What was not done:** the exact cross-check against the machine's external IP (runbook section I) was not
  completed, because of an unrelated local Windows TLS trust failure. This is an observation, not a proof. The
  section I check remains required before any customer is moved (A4).

## 3.4 Left in production by the test

- **"SortView Admin Smoke Test":** **Cancelled**. Offboarding is not a purge: its rows remain, and deleting them is a
  separate operator decision (`scripts/purge_tenant_data.py`).
- **"Rehearsal Library":** its routing-review marker now carries the smoke test's time. Its installation name was
  restored.

## 3.5 Result

**PASSED.** React admin is hosted in production and A2 items 1-7 work there. This clears the production-hosting
prerequisite for A2 item 8 (retiring Super Admin Streamlit).

Super Admin Streamlit stayed live as the temporary fallback until item 8's retirement sequence ran. The hosted app was
deleted on 2026-10-15 (its old URL no longer serves the app; customer Streamlit still works), and item 8's code
removal was then merged (PR #212; see `docs/PROJECT_MANIFEST.md`).

---

# Part 4 -- Production React customer app verification (A4)

The hosting is described in [`react-hosting-runbook.md`](react-hosting-runbook.md), section M. This part records the
NBPL customer React production cutover. Recorded 2026-10-16.

No credential, session cookie, token, IP address or personal data is recorded here, and none may be added.

## 4.1 What was under test

| Item | Value |
|---|---|
| Customer app | `https://app.sortviewapp.com`: the existing customer CloudFront distribution (first set up as the trial) and its existing private S3 bucket (OAC), with the existing ACM certificate attached |
| DNS | Cloudflare `CNAME` from `app.sortviewapp.com` to the customer distribution, DNS only |
| Frontend artifact | `npm ci` + `npm run build` → `frontend/dist`, built from `main` at `2a8e40f`; uploaded and invalidated as in the hosting runbook, M.4 |
| Backend | DigitalOcean `sortview-app-2p336.ondigitalocean.app`. The environment change redeployed it, and it was healthy afterwards |
| Allowed origins | `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` includes `https://app.sortviewapp.com` and `https://admin.sortviewapp.com`, and temporarily the trial origin; `SORTVIEW_CUSTOMER_APP_URL` = `https://app.sortviewapp.com`; `SORTVIEW_ADMIN_ALLOWED_ORIGINS` = `https://admin.sortviewapp.com`, unchanged |

## 4.2 Results

**Build**

| Check | Result |
|---|---|
| Customer bundle boundary: no `/api/admin`, no "SortView Administration" | PASS |

**Hosting and session**

| Check | Result |
|---|---|
| `https://app.sortviewapp.com` served over HTTPS on the custom hostname | PASS |
| Sign in | PASS |
| NBPL's organization and sorter visible | PASS |
| Sign out, then sign in again | PASS |
| Deep route loaded directly, then refreshed | PASS |

**Customer features (NBPL)**

| Check | Result |
|---|---|
| Live Today | PASS |
| Reports, including historical reporting | PASS |
| Transits, rejects and holds | PASS |
| CSV download and print | PASS |
| Settings, Members and My Account | PASS. These pages' writes carry the browser's Origin, so they passed the customer Origin guard with the new origin |

**Regression**

| Check | Result |
|---|---|
| `https://admin.sortviewapp.com` | PASS: remained healthy |
| Customer Streamlit app | PASS: remained healthy |
| Collectors and pipeline | PASS: remained healthy |

## 4.3 Client IP verification

**PASSED.** For real customer `/api` traffic through `https://app.sortviewapp.com`, the viewer's external IP matched the
client IP in DigitalOcean's Uvicorn access lines (hosting runbook section I). The current customer CloudFront path
therefore keeps `request.client.host`, and the per-IP rate limits keyed on it, working. This completes the check
that A3 could only observe (Part 3, section 3.3). The address itself is not recorded.

## 4.4 Not re-tested in A4

- **A forged-Origin request was not re-tested on `app.sortviewapp.com`.** The guard is the same code path that refused
  a forged Origin with 403 `origin_not_allowed` on the admin distribution (Part 3) and that the automated tests cover;
  that is established behavior, not part of this live smoke test.
- **Only NBPL** was moved. No other customer was part of this cutover.

## 4.5 Left in place

- **The customer Streamlit app** (`https://sortview.streamlit.app/`) was left live as a temporary rollback target.
  A5 retired it (Part 5).
- **The trial CloudFront origin** was left in `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` for the soak. It turned out to be
  required by production and must stay (Part 5).
- No customer data migration was needed: the React and Streamlit apps operated on the same production data.

## 4.6 Result

**PASSED.** The NBPL customer React cutover is complete (A4, 2026-10-16). `https://app.sortviewapp.com` is the
production customer entry point. The customer Streamlit app was kept as rollback until A5 retired it (Part 5).

---

# Part 5 -- Retirement of the customer Streamlit app (A5)

This part records the production steps of A5. Recorded 2026-10-17. The source, test and dependency changes are in the
repository (`docs/PROJECT_MANIFEST.md`, A5).

No credential, session cookie, token, IP address or personal data is recorded here, and none may be added.

## 5.1 Production steps

| Step | Result |
|---|---|
| Admin-lock inventory (read-only) | No active admin-lock credential: no non-empty plaintext `admin_password` and no `admin_password_hash` in any row. Four `organization_settings` rows held an obsolete `security` block (nbpl, sortview-clean-install-test, test-install, rehearsal-library); `branch_settings` held none |
| Guest account | The historical guest account was deactivated through the application's own account service (`user_admin_service.set_global_account_active`, platform-administrator actor; sessions revoked and an audit row written). It had no active membership |
| Hosted customer Streamlit app | Deleted from Streamlit Community Cloud after pre-checks of the customer, admin, backend and collector paths. Its old URL no longer serves the app. Its database credential was the backend's shared runtime role, so it was kept, not revoked |
| Stale `security` JSON | Removed from exactly those four `organization_settings` rows in one guarded transaction (exact-shape checks, row locks, a four-row count and before/after fingerprints). Other settings in those rows, every other `organization_settings` row and `branch_settings` were verified unchanged |
| After the cleanup | The customer app's smoke check passed |

## 5.2 Generated CloudFront origin: required

Removing the customer distribution's generated hostname, `https://d22i2vjcxygt0c.cloudfront.net`, from `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS`
after the A4 soak was tried in production. Customer writes from `https://app.sortviewapp.com` then failed with
"Request origin is not allowed.", and restoring the hostname immediately restored normal behavior. The cause is not yet
explained. Production's customer list must keep `https://app.sortviewapp.com`, `https://admin.sortviewapp.com` and
`https://d22i2vjcxygt0c.cloudfront.net` (hosting runbook, M.5).

## 5.3 Result

**COMPLETE.** The React customer app is the only customer UI. The customer Streamlit app, its source, its dependencies,
the admin lock and its stale production data are gone. Customer rollback is the hosting runbook's section M.8.
