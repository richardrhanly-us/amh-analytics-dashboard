# React hosting runbook: the production customer app and the production admin app

How the React customer app (`frontend/`) is hosted behind Amazon CloudFront, with its `/api/*` requests sent to the
existing FastAPI service.

- **Customer app: in production at `https://app.sortviewapp.com`** since the A4 cutover (2026-10-16). It runs on the
  customer distribution and bucket that were first set up as a trial on CloudFront's generated hostname. Sections A
  to J record that setup and still describe the distribution. Section K is the custom-domain step, and **section M**
  is the current production record, deployment and rollback.
- **Admin app** (`frontend/admin/`, built to `frontend/dist-admin`): in production at
  **`https://admin.sortviewapp.com`** on its own distribution and bucket, built the same way (**section L**).

> **The customer Streamlit app, <https://sortview.streamlit.app/>, is still live** as a temporary rollback target
> during the transition. Retiring it is A5 (`docs/PROJECT_MANIFEST.md`).

```
Browser ──► https://app.sortviewapp.com, or the generated https://<DISTRIBUTION_DOMAIN>   (CloudFront, global)
              ├── default behavior  ──► private S3 bucket (the built React app)
              └── /api/*            ──► https://sortview-app-2p336.ondigitalocean.app (FastAPI, unchanged)
```

The browser only ever talks to the customer app's hostname. The app requests `/api/...` on its own origin, so
the session cookie and the API's Origin check work exactly as they do in local development behind the
Vite proxy. Collectors and agents keep calling the DigitalOcean hostname directly, and none of their paths
are under `/api`.

Placeholders used below, all filled in from the AWS console as the resources are created. Never commit
real values into this file:

| Placeholder | Meaning |
| --- | --- |
| `<BUCKET>` | the S3 bucket's name |
| `<ACCOUNT_ID>` | the AWS account id |
| `<DISTRIBUTION_ID>` | the CloudFront distribution's id (for example `E2ABC…`) |
| `<DISTRIBUTION_DOMAIN>` | its generated hostname (for example `d1234abcd.cloudfront.net`) |

---

## A. Preconditions

- **AWS access** to the account that will hold the bucket and the distribution, with permission to manage
  S3, CloudFront (including CloudFront Functions and Origin Access Control) and, for the upload, the AWS CLI.
- **The AWS CLI is used from an approved, non-city machine.** Credentials never go into this repository,
  a script, or a shell history you will share.
- **Region:** the bucket is created in **us-east-1**. CloudFront itself is global, with no region to choose.
- **No custom domain** was needed for the original trial: the distribution's generated `*.cloudfront.net` hostname
  was the address. Production adds `app.sortviewapp.com` (sections K and M).
- **Backend origin:** `sortview-app-2p336.ondigitalocean.app`, the existing FastAPI service. It is not
  changed by this runbook, except for two environment values in section F.
- **Node.js** matching `frontend/.nvmrc` (24), to build the app.

## B. Build contract

From the repository root:

```powershell
cd frontend
npm ci
npm run build
```

- `npm ci` installs exactly what `package-lock.json` records. `npm run build` runs `tsc -b && vite build`.
- The output is **`frontend/dist`**: `index.html` plus `assets/index-<hash>.js` and
  `assets/index-<hash>.css`. There are no other files: no `public/` folder, and the page uses an inline
  icon, so no `/favicon.ico` is requested. `dist/` is git-ignored and is never committed.
- **`VITE_API_BASE_URL` must be empty or unset** for this build. Every `VITE_*` value is compiled into the
  JavaScript. Empty means the app calls `/api/...` on whatever hostname served it: the CloudFront hostname.
  Never point it at the DigitalOcean hostname. A hard-coded backend address in shipped code is also refused
  by `frontend/src/test/scope.test.ts`.
- **`DEV_API_PROXY_TARGET` is development only.** It configures the Vite dev server's proxy, is not a `VITE_`
  variable, and has no effect on `npm run build`. Nothing in production proxies through Vite.
- Build from a clean checkout of the commit being deployed (`git status` clean), and keep a copy of that
  `dist/` folder, named by commit, as a rollback artifact.

## C. S3 bucket

Create one bucket for the built app:

1. **Region:** us-east-1. **Name:** your choice (bucket names are global). Record it as `<BUCKET>`.
2. **Block Public Access:** all four settings **on**. The bucket is never public.
3. **Static website hosting:** **off**.
4. **Bucket versioning:** **on**. Every upload of `index.html` keeps the previous version, which is the
   fastest rollback (section J).
5. Default encryption (SSE-S3) is fine. No other features are needed.

**Why the S3 REST origin and not the website endpoint:** the website endpoint is plain HTTP and only works
for a publicly readable bucket. The REST endpoint (`<BUCKET>.s3.us-east-1.amazonaws.com`) is HTTPS, and
with Origin Access Control only this CloudFront distribution can read it. SPA routing does not need the
website endpoint's index or error documents: the CloudFront Function in section D does that job.

## D. CloudFront distribution

### The SPA routing function (create first)

1. CloudFront → Functions → Create function. Runtime **cloudfront-js-2.0**.
2. Paste the source of **[`infra/cloudfront/spa-routing.js`](../infra/cloudfront/spa-routing.js)**, as
   committed and unchanged.
3. Use **Test** with viewer-request events for `/organizations/x/reports` (expect `/index.html`),
   `/assets/index-abc.js` (unchanged), `/api/auth/session` (unchanged) and `/` (unchanged).
4. **Publish** it.

It rewrites every request on the default behavior to `/index.html`, except `/`, `/assets/*` and `/api/*`.
It never redirects and leaves query strings, cookies and headers alone.

### Origins

**Origin A, the app:**

- Origin domain: the bucket's REST endpoint, `<BUCKET>.s3.us-east-1.amazonaws.com`. Not the website endpoint.
- **Origin access: Origin Access Control.** Create a new OAC for S3, signing behavior **Sign requests
  (recommended)**.

**Origin B, the API:**

- Origin domain: **`sortview-app-2p336.ondigitalocean.app`** (custom origin).
- Protocol: **HTTPS only**, port 443. Minimum origin SSL protocol: **TLSv1.2**.
- **No custom origin headers**, no origin secret.

### Distribution settings

- **Default root object: `index.html`.**
- Alternate domain names: **none** for the original trial, with the default CloudFront certificate. Since A4:
  `app.sortviewapp.com`, with the ACM certificate (section M).
- **Custom error responses: none.**

> **DO NOT add distribution-wide 403/404 → `/index.html` custom error responses.**
>
> They apply to EVERY behavior, `/api/*` included. The API answers with JSON 404s and 403s all the time
> (`tenant_not_found`, `organization_not_found`, `feature_not_available`, `origin_not_allowed`). With such a
> rule those become the app's HTML page with a 200 or a rewritten status, and the app can no longer tell
> "not found" from "not allowed". SPA routing is done ONLY by the function, on the default behavior ONLY.

### Default behavior (`*`, goes to Origin A)

- Viewer protocol policy: **Redirect HTTP to HTTPS**.
- Allowed methods: **GET, HEAD**.
- **Compress objects automatically: yes.**
- Cache policy: **Managed-CachingOptimized**. It honors the `Cache-Control` set on upload (section G).
- Origin request policy: **none**. No viewer cookies, query strings or headers are needed by S3.
- Function associations: **Viewer request → CloudFront Function → the published SPA routing function.**

### `/api/*` behavior (goes to Origin B)

- Path pattern: **`/api/*`**.
- Viewer protocol policy: **Redirect HTTP to HTTPS**.
- Allowed methods: **GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE**.
- Cache policy: **Managed-CachingDisabled**. Every API response is private to one session.
- Origin request policy: **Managed-AllViewerExceptHostHeader**, or its current equivalent. That forwards
  every viewer cookie (the session cookie), every query string (report ranges), every viewer header
  (`Origin`, which the API's CSRF check reads; `Content-Type`), and bodies. It sends the **origin's**
  `Host` (`sortview-app-2p336.ondigitalocean.app`), which DigitalOcean needs to route the request and serve
  the right certificate. The app reads no `Host` header.
- Function associations: **none.** No SPA function and no error rewriting on this behavior.
- Compression: optional. The API's responses are small JSON.

Create the distribution and wait until it is **Deployed**. Record `<DISTRIBUTION_ID>` and
`<DISTRIBUTION_DOMAIN>`.

## E. OAC bucket policy

Only the distribution may read the bucket, and only objects. CloudFront offers to copy a policy like this
when the OAC is attached. It must have exactly these semantics, with the placeholders filled in:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowCloudFrontReadOfTheReactAppOnly",
      "Effect": "Allow",
      "Principal": { "Service": "cloudfront.amazonaws.com" },
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<BUCKET>/*",
      "Condition": {
        "StringEquals": {
          "AWS:SourceArn": "arn:aws:cloudfront::<ACCOUNT_ID>:distribution/<DISTRIBUTION_ID>"
        }
      }
    }
  ]
}
```

Without `s3:ListBucket`, a request for a file that does not exist gets S3's **403 AccessDenied**, not a
404. That is expected, and section H relies on it being an S3 error rather than the app's HTML.

## F. Backend environment for the trial

The production values are in section M.5.

Login through the CloudFront hostname is refused until DigitalOcean's backend allows that origin. In the
DigitalOcean app's environment, before the authentication tests:

```
SORTVIEW_CUSTOMER_ALLOWED_ORIGINS=<existing value, if any>,https://<DISTRIBUTION_DOMAIN>
SORTVIEW_CUSTOMER_APP_URL=https://<DISTRIBUTION_DOMAIN>
```

- **Record both variables' current values first**, privately and not in this repository. Section J restores them.
- `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` is a comma-separated list of exact origins (`https://host`, no path,
  no trailing slash). **Add** the trial origin and keep any origin that is still needed. Don't replace the list.
- `SORTVIEW_CUSTOMER_APP_URL` is the origin that password-reset emails link to
  (`<that origin>/reset-password#token=…`). It must also be in the allowed list, or reset requests answer 503.
- **Leave `SORTVIEW_CUSTOMER_COOKIE_SECURE` unset** (secure). The trial is HTTPS end to end.
- **Leave `SORTVIEW_ALLOWED_ORIGINS` (CORS) alone.** The app's requests are same-origin, so CORS never
  applies to them. Change it only for a verified cross-origin use, which this trial is not.
- **Do not change collector or agent configuration, or the Streamlit app's settings.** Streamlit's own reset
  links use `SORTVIEW_APP_URL`, which is untouched.

Saving environment changes redeploys the DigitalOcean app. Check `https://sortview-app-2p336.ondigitalocean.app/`
still answers `{"status": "SortView API running"}` afterward.

## G. Upload

From `frontend/`, after section B's build. PowerShell:

```powershell
# 1. FIRST: the hashed assets. Their names change whenever their content does, so they can be cached for a year.
aws s3 sync dist/assets s3://<BUCKET>/assets `
  --cache-control "public,max-age=31536000,immutable"

# 2. LAST: index.html, the one file that names the current assets. It must never be cached for long.
aws s3 cp dist/index.html s3://<BUCKET>/index.html `
  --cache-control "no-cache"

# 3. Make CloudFront fetch the new index.html now rather than at its next revalidation.
aws cloudfront create-invalidation `
  --distribution-id <DISTRIBUTION_ID> `
  --paths "/index.html" "/"
```

- **Order matters.** Assets first, `index.html` last, so the page never names a file that isn't there yet.
- **Do not use `--delete`** for the initial deployments. Old hashed assets are harmless: nothing names them
  once the new `index.html` is live. They let a browser that loaded the previous `index.html` finish
  loading, and they make rollback to that `index.html` work instantly. Pruning them is a later, separate chore.
- Plain `aws s3 sync` without `--cache-control` stores no `Cache-Control`. CloudFront would then keep
  `index.html` for its default TTL (a day), so always use the commands above.

## H. Trial smoke tests

Against `https://<DISTRIBUTION_DOMAIN>`, in a private browser window with dev tools open, plus `curl.exe` from
PowerShell. Use a test account and test organizations. Record each result, pass or fail, with the commit deployed.

### Before signing in

| Check | Expected |
| --- | --- |
| Open `/` | The app's sign-in page. |
| Open `/organizations/<org>/sorters/<sorter>/reports` directly, then refresh | The app (sign-in, then that page). Never a 403 or 404 from S3. |
| Open `/reset-password` | The app's reset page. |
| `curl.exe -i https://<DISTRIBUTION_DOMAIN>/assets/missing.js` | **403 from S3** (XML `AccessDenied`). Not the app's HTML. |
| `curl.exe -i https://<DISTRIBUTION_DOMAIN>/api/auth/session` | **401** JSON `{"code":"not_authenticated",…}`, `Cache-Control: no-store`. |
| `curl.exe -i https://<DISTRIBUTION_DOMAIN>/api/nonexistent` | **404 JSON** from FastAPI. Not `index.html`. |

### Signing in

| Check | Expected |
| --- | --- |
| Sign in | Lands in the app, with the user's organizations listed. |
| Dev tools → Application → Cookies | `__Host-sortview_api_session`: **Secure, HttpOnly, SameSite=Lax, Path=/**, Domain shown as the CloudFront host only, no Domain attribute set. |
| Refresh the page | Still signed in. |
| Open a new tab at the same address | Still signed in. |
| Sign out | Back to sign-in. The cookie is gone. `/api/auth/session` answers 401 again. |
| Forged Origin (below) | **403** `{"code":"origin_not_allowed",…}`. |
| Request a password reset for the test account | The email's link begins `https://<DISTRIBUTION_DOMAIN>/reset-password#token=`. |

The forged Origin request:

```powershell
'{"email":"nobody@example.invalid","password":"not-a-password"}' | Set-Content -Encoding ascii body.json
curl.exe -i -X POST "https://<DISTRIBUTION_DOMAIN>/api/auth/login" `
  -H "Origin: https://forged.example.invalid" -H "Content-Type: application/json" --data-binary "@body.json"
Remove-Item body.json
```

### Reports and settings

| Check | Expected |
| --- | --- |
| A sorter's **Live Today** | Figures load and refresh. |
| Sorter reports, **Last 30 days** | Every section loads. |
| A custom range **longer than 92 days** | Loads. The daily charts and tables show **months**. Cards are unchanged in kind. |
| **Holds**, range of 92 days or fewer (organization with Holds) | Public and ILL counts. |
| **Holds**, range over 92 days | The note "Holds reporting is currently available for ranges up to 92 days." No error, and no holds request in the network tab. |
| **Organization reports** | Overview, Sorter comparison, Routing network (with transits), System reliability. |
| **Transits capability** | An organization without transits shows no Routing anywhere. Requests to its routing addresses answer 403. |
| **Efficiency** | Shown to owners and admins only. |
| **Users & Access** | Lists members. An admin's change applies. |
| **Account** | Name change saves. |
| **Settings → Routing**: change and **Save** (a PUT) | Saved, and still there after a refresh. |

### Security

| Check | Expected |
| --- | --- |
| A **suspended** test organization | Reports readable. Settings and member changes refused. |
| A **cancelled** test organization | Not found. |
| Another organization's addresses (edit the URL) | Not found. |
| Response headers of `/api/*` requests | `Cache-Control: no-store`, and `X-Cache` never `Hit from cloudfront`. |
| Two different users in two browsers | Each sees only their own organizations and figures, never the other's. |

## I. Client IP verification

After making customer API requests through CloudFront (the steps above), open the DigitalOcean app's
**runtime logs** and read Uvicorn's access lines for those `/api/...` requests. Production runs
`uvicorn main:app --host 0.0.0.0 --port 8080`, and today those lines show the viewer's real public IP.

- **Still the viewer's real public IP**, the same address you see when calling the DigitalOcean hostname
  directly from the same machine: record it, and the trial can continue.
- **A repeated CloudFront or proxy address instead**, the same few addresses whoever is calling:
  **STOP before any customer is moved** (or, in production, before more traffic depends on it).

**Why it matters.** The customer API's login and password limits (SlowAPI) key on `request.client.host`. If
that becomes a CloudFront address, many customers share one rate-limit identity: one person's failed
logins could lock everyone out for a minute. The fix would be a backend change to make the limiter
proxy-aware. Its design depends on what the logs show, so it is not specified here and must be built and
reviewed before any customer is moved.

**Production result (A4, 2026-10-16): PASSED** on the customer path through `app.sortviewapp.com`. The viewer's
external IP matched the client IP in DigitalOcean's Uvicorn access lines for real customer `/api` requests, so the
current CloudFront path keeps `request.client.host`, and the per-IP limits keyed on it, working
(`docs/production-verification-runbook.md` Part 4). The address itself is not recorded.

## J. Rollback

| Problem | Do this |
| --- | --- |
| **Bad frontend build** | Restore the previous `index.html`: S3 → the object → Versions → restore the prior version, or `aws s3 cp` the saved `dist/index.html` of the previous commit with `--cache-control "no-cache"`. Invalidate `/index.html` and `/`. The previous hashed assets are still in the bucket (no `--delete`), so it works at once. |
| **`/api/*` behaving wrongly** (HTML instead of JSON, cookies missing, 403s) | Correct or revert the `/api/*` behavior's settings to section D. Check no custom error responses exist. In production, see section M.8: customers can return to Streamlit while it is fixed. |
| **Login or origin configuration wrong** | Restore the two DigitalOcean values recorded in section F. Streamlit and the collectors never read them and are untouched. |
| **The trial hostname** | It is the same distribution as production now. Do not disable the distribution: that takes `app.sortviewapp.com` down with it. |

## K. The customer app's custom domain (done in A4)

> SortView uses the domain **`sortviewapp.com`**, with its DNS on Cloudflare. An ACM certificate in us-east-1 covers
> **`app.sortviewapp.com`** and `admin.sortviewapp.com`. These steps were carried out for the customer distribution in
> the A4 cutover on 2026-10-16. Section M records the result, and section L.4 the same steps for the admin app.

1. **Done:** the domain is `sortviewapp.com`, with DNS on Cloudflare.
2. **Done:** an ACM certificate in **us-east-1** (CloudFront only uses certificates from that region), validated by
   DNS. It covers `app.sortviewapp.com` and `admin.sortviewapp.com` and is **Issued**. Its validation CNAMEs stay in
   Cloudflare so it can renew.
3. **Done:** the hostname is in the distribution's **alternate domain names**, with the certificate selected.
4. **Done:** in Cloudflare, the hostname is a CNAME to `<DISTRIBUTION_DOMAIN>` set to **DNS only** (not proxied), as
   for the admin app (L.4).
5. **Done:** DigitalOcean has `https://app.sortviewapp.com` in `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS`, and
   `SORTVIEW_CUSTOMER_APP_URL=https://app.sortviewapp.com`. The session cookie is per hostname, so users sign in
   again on the new name.
6. **Done:** section H repeated on the new hostname, including section I.

---

## L. Production: the admin app at `https://admin.sortviewapp.com`

In production since 2026-10-15 (A3), for platform administrators only. It is the same pattern as sections A to G,
on **its own private bucket and its own CloudFront distribution**, separate from the customer app's (section M).

```
Browser ──► https://admin.sortviewapp.com   (Cloudflare DNS, CNAME, DNS only ──► the admin distribution)
              ├── default behavior  ──► private S3 bucket (frontend/dist-admin)
              └── /api/*            ──► https://sortview-app-2p336.ondigitalocean.app (origin "sortview-fastapi-prod")
```

Placeholders, as in the rest of this runbook. The real values are in the AWS console and are not committed:

| Placeholder | Meaning |
| --- | --- |
| `<ADMIN_BUCKET>` | the admin app's bucket (its name contains the AWS account id) |
| `<ADMIN_DISTRIBUTION_ID>` | the admin distribution's id |
| `<ADMIN_DISTRIBUTION_DOMAIN>` | its generated `*.cloudfront.net` hostname, the target of the DNS record |

### L.1 Why a separate distribution and hostname

For the current SortView security model and frontend builds, the admin app uses a separate distribution and hostname:

- **The admin write guard needs its own origin.** Every non-GET/HEAD `/api/admin` request must carry an `Origin`
  listed in `SORTVIEW_ADMIN_ALLOWED_ORIGINS` (`src/admin_api/dependencies.py`, `require_admin_origin`). That keeps
  a page served from the customer app's origin from ever sending an admin write. Admin on the customer host, for
  example under `/admin`, would make the two origins the same and defeat the guard.
- **Both builds are root-based.** Each serves `/assets/index-<hash>.*`, and neither router uses a basename. With the
  current root-based builds, they are not designed to share one bucket root or hostname without changing the
  build/router layout.
- **The session cookie is host-only.** `__Host-sortview_api_session` is Secure, HttpOnly, SameSite=Lax, Path=/ and
  carries no Domain (`src/customer_api/errors.py`). The admin host therefore has its own session: an administrator
  signs in there separately, and no cookie-domain change is needed.
- **The API is same-origin.** The shared client sends `credentials: 'same-origin'` to `/api/...` on the page's own
  host (`frontend/src/api/client.ts`), so each distribution needs its own `/api/*` behavior.

### L.2 Bucket

The admin bucket is separate from the customer app's, in us-east-1:

- private, with **Block Public Access** on;
- **Bucket owner enforced** (ACLs disabled);
- **versioning** on;
- **SSE-S3**;
- **static website hosting** off.

Only the admin distribution may read it: the policy has the semantics of section E, with `<ADMIN_BUCKET>` and
`<ADMIN_DISTRIBUTION_ID>`.

### L.3 Distribution

The distribution is on CloudFront's free flat-rate plan.

- **Default root object:** `index.html`.
- **Custom error responses:** none. The rule in section D applies here too.
- **Default behavior (`*`), to the bucket's private S3 REST origin:**
  - Redirect HTTP to HTTPS;
  - Managed-CachingOptimized;
  - the existing CloudFront Function **`sortview-spa-routing`** (`infra/cloudfront/spa-routing.js`), associated
    on **Viewer request only**.

  The function needs no change for this app. The admin routes (`/libraries`, `/libraries/:slug`, `.../collector`,
  `.../installations`, `.../lifecycle`, `.../sorters/:sorter`, `/provision`) are not under `/assets/` or `/api/`,
  so they are served `/index.html`, and `/` uses the default root object.
- **`/api/*` behavior, ordered ahead of the default behavior, to origin `sortview-fastapi-prod`
  (`sortview-app-2p336.ondigitalocean.app`):**
  - origin connection: HTTPS only, port 443, minimum origin TLS **TLSv1.2**;
  - Redirect HTTP to HTTPS;
  - methods GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE;
  - **Managed-CachingDisabled**;
  - **Managed-AllViewerExceptHostHeader**;
  - no function association.

  These are the same settings as the trial's `/api/*` behavior in section D.

### L.4 Domain and TLS

- **Alternate domain name:** `admin.sortviewapp.com`, using the ACM certificate in **us-east-1** that covers
  `app.sortviewapp.com` and `admin.sortviewapp.com`. The certificate is **Issued**.
- **Cloudflare DNS:** a `CNAME` from `admin.sortviewapp.com` to `<ADMIN_DISTRIBUTION_DOMAIN>`, set to **DNS only**
  (not proxied), so CloudFront terminates TLS with the ACM certificate.
- **Certificate validation:** the ACM validation CNAMEs stay in Cloudflare. ACM needs them to renew the
  certificate. Do not delete them.
- **`app.sortviewapp.com`:** covered by the same certificate and attached to the customer distribution in A4
  (section M).

### L.5 Build and upload

Build from a clean checkout of the commit being deployed (Node per `frontend/.nvmrc`). Keep the `dist-admin` folder,
named by commit, as the rollback artifact. PowerShell, from `frontend/`:

```powershell
npm ci
npm run build:admin          # tsc -b tsconfig.admin.json && vite build --config vite.admin.config.ts -> dist-admin/

# 1. FIRST: the hashed assets, cached for a year.
aws s3 sync .\dist-admin\assets\ s3://<ADMIN_BUCKET>/assets/ `
  --cache-control "public,max-age=31536000,immutable"

# 2. LAST: index.html, never cached for long.
aws s3 cp .\dist-admin\index.html s3://<ADMIN_BUCKET>/index.html `
  --cache-control "no-cache"

# 3. Invalidate.
aws cloudfront create-invalidation `
  --distribution-id <ADMIN_DISTRIBUTION_ID> `
  --paths "/" "/index.html"
```

- The build is `dist-admin/index.html` plus hashed CSS and JS under `dist-admin/assets/`.
- `VITE_API_BASE_URL` stays empty or unset (section B).
- Do not use `--delete`, for the same reasons as section G.
- The rules of section G apply unchanged: assets first, `index.html` last, always with the cache-control values
  above.

### L.6 Backend environment (DigitalOcean)

Production holds:

```
SORTVIEW_CUSTOMER_ALLOWED_ORIGINS=https://app.sortviewapp.com,https://admin.sortviewapp.com,<during the A4 soak only: the customer trial origin, https://<DISTRIBUTION_DOMAIN>>
SORTVIEW_ADMIN_ALLOWED_ORIGINS=https://admin.sortviewapp.com
```

- **The admin origin must be in BOTH lists.**
  - The admin app signs in and out through `POST /api/auth/login` and `/api/auth/logout`. Those routes check only
    the customer list (`require_allowed_origin`, `src/customer_api/auth_dependencies.py`).
  - Every admin write checks only the admin list (`require_admin_origin`).
  - A value missing from the customer list breaks sign-in. A value missing from the admin list refuses every admin
    write with 403 `origin_not_allowed`.
- **Leave these alone:**
  - `SORTVIEW_CUSTOMER_APP_URL` belongs to the customer app (`https://app.sortviewapp.com`, section M.5). It is the
    address password-reset links use, and the admin app has no reset page.
  - `SORTVIEW_CUSTOMER_COOKIE_SECURE` stays unset (secure).
  - `SORTVIEW_ALLOWED_ORIGINS` (CORS) is unchanged: the app's requests are same-origin.
- **Record the current values privately before any change** (section F). Saving environment values redeploys the
  backend; confirm afterwards that the deployed commit is unchanged and that `GET /` still answers.

### L.7 Verification

- The production smoke test and its recorded result are in
  [`production-verification-runbook.md`](production-verification-runbook.md), Part 3.
- After every later admin deployment, repeat at least:
  - sign-in, refresh and sign-out;
  - a direct load and refresh of `/libraries/<slug>/installations`;
  - `/api/admin/session` answering 401 JSON when signed out;
  - a forged-Origin POST answering 403 `origin_not_allowed`;
  - a check that the customer app (`app.sortviewapp.com`), the customer Streamlit app while it remains (until A5)
    and the collectors are unaffected.

### L.8 Rollback

| Problem | Do this |
| --- | --- |
| **Bad admin build** | Put back the previous known-good build: restore the prior `index.html` version (versioned bucket), or upload the saved `dist-admin` of the previous commit with the commands in L.5. Invalidate `/` and `/index.html`. The previous hashed assets are still in the bucket (no `--delete`). |
| **Allowed-origin values wrong** (sign-in refused, or admin writes 403 `origin_not_allowed`) | Restore the DigitalOcean values recorded before the change and let the backend redeploy. Streamlit and the collectors never read them. |
| **CloudFront behavior wrong** (HTML instead of JSON, missing cookies, S3 errors on app routes) | Correct the behavior, origin or function association to L.3. Invalidate the affected paths if cached content is wrong. |
| **Auth or cookie problem** | Check the cookie attributes (L.1) and the `/api/*` origin request policy, then the allowed-origin values. |

- **Before A2 item 8 retires Super Admin Streamlit,** Super Admin remains the temporary fallback for platform
  administration.
- **After item 8,** rollback is this section: artifact, configuration and CloudFront rollback. Restoring Super
  Admin is not the rollback path.

## M. Production: the customer app at `https://app.sortviewapp.com`

In production since **2026-10-16** (A4, the NBPL customer React cutover). It is the production customer entry point.
The customer Streamlit app stays live as a temporary rollback target until A5.

```
Browser ──► https://app.sortviewapp.com   (Cloudflare DNS, CNAME, DNS only ──► the customer distribution)
              ├── default behavior  ──► private S3 bucket (frontend/dist)
              └── /api/*            ──► https://sortview-app-2p336.ondigitalocean.app
```

The placeholders `<BUCKET>`, `<DISTRIBUTION_ID>` and `<DISTRIBUTION_DOMAIN>` are those of sections A to J: the real
values are in the AWS console and are not committed.

### M.1 What was reused

- **The existing customer distribution**, first set up as the trial (section D), unchanged except for its alternate
  domain and certificate.
- **The existing private S3 bucket** (section C), still read only through Origin Access Control (section E).
- **The existing ACM certificate** in us-east-1, which covers `app.sortviewapp.com` and `admin.sortviewapp.com`.

No customer data migration was needed: both apps operate on the same production database. The React app reaches it
through FastAPI; the Streamlit app reads it directly.

### M.2 Distribution

As section D, with:

- **Alternate domain name:** `app.sortviewapp.com`, with the ACM certificate above.
- **Default root object:** `index.html`. **Custom error responses:** none (the rule in section D).
- **Default behavior (`*`), to the private S3 REST origin with OAC:** Redirect HTTP to HTTPS; GET, HEAD; compression
  on; Managed-CachingOptimized; the CloudFront Function **`sortview-spa-routing`** (`infra/cloudfront/spa-routing.js`)
  on **Viewer request only**.
- **`/api/*` behavior, ahead of the default, to `sortview-app-2p336.ondigitalocean.app`:** HTTPS only, port 443,
  minimum origin TLS TLSv1.2; Redirect HTTP to HTTPS; GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE;
  **Managed-CachingDisabled**; **Managed-AllViewerExceptHostHeader**; no function.

### M.3 Domain and TLS

- **Cloudflare DNS:** a `CNAME` from `app.sortviewapp.com` to `<DISTRIBUTION_DOMAIN>`, set to **DNS only** (not
  proxied), so CloudFront terminates TLS with the ACM certificate.
- **Certificate validation:** the ACM validation CNAMEs stay in Cloudflare so the certificate can renew (L.4).

### M.4 Build and upload

Build from a clean checkout of the commit being deployed (Node per `frontend/.nvmrc`). Keep the `dist` folder, named by
commit, as the rollback artifact. PowerShell, from `frontend/`:

```powershell
npm ci
npm run build                # tsc -b && vite build -> dist/

# Boundary check (the same rule as frontend CI): no admin code in the customer bundle. It must print nothing.
Get-ChildItem .\dist -Recurse -File | Select-String -SimpleMatch -Pattern '/api/admin', 'SortView Administration'

# 1. FIRST: the hashed assets, cached for a year.
aws s3 sync .\dist\assets\ s3://<BUCKET>/assets/ `
  --cache-control "public,max-age=31536000,immutable"

# 2. LAST: index.html, never cached for long.
aws s3 cp .\dist\index.html s3://<BUCKET>/index.html `
  --cache-control "no-cache"

# 3. Invalidate.
aws cloudfront create-invalidation `
  --distribution-id <DISTRIBUTION_ID> `
  --paths "/" "/index.html"
```

- `VITE_API_BASE_URL` stays empty or unset (section B).
- Do not use `--delete` (section G). Assets first, `index.html` last, always with the cache-control values above.
- An invalidation of `/` and `/index.html` is needed on every deployment.

### M.5 Backend environment (DigitalOcean)

Production holds:

```
SORTVIEW_CUSTOMER_ALLOWED_ORIGINS=https://app.sortviewapp.com,https://admin.sortviewapp.com,<during the A4 soak only: https://<DISTRIBUTION_DOMAIN>>
SORTVIEW_CUSTOMER_APP_URL=https://app.sortviewapp.com
SORTVIEW_ADMIN_ALLOWED_ORIGINS=https://admin.sortviewapp.com
```

- **`https://app.sortviewapp.com` must be in the customer list.** Customer sign-in, sign-out and every customer write
  check that list (`require_allowed_origin`).
- **`SORTVIEW_CUSTOMER_APP_URL` must be `https://app.sortviewapp.com`**, and must also be in the customer list, or
  password-reset requests answer 503. Reset emails link to `https://app.sortviewapp.com/reset-password#token=…`.
- **`https://admin.sortviewapp.com` stays in the customer list** as well as the admin list (L.6).
- **The trial origin** stays in the customer list only for the A4 soak, and is removed after it (A5).
- Leave `SORTVIEW_CUSTOMER_COOKIE_SECURE` unset, and `SORTVIEW_ALLOWED_ORIGINS` (CORS) unchanged.
- Record the current values privately before any change. Saving environment values redeploys the backend; confirm
  afterwards that it is healthy and the deployed commit is unchanged.

### M.6 The cutover that was carried out (2026-10-16)

1. Built the customer bundle from `main` at `2a8e40f`, and checked it contains no `/api/admin` and no
   "SortView Administration".
2. Uploaded it to the existing customer bucket, assets first and `index.html` last, and invalidated `/` and
   `/index.html`.
3. Attached the alternate domain `app.sortviewapp.com` and the existing ACM certificate to the existing customer
   distribution.
4. Updated DigitalOcean: added `https://app.sortviewapp.com` to `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` (keeping
   `https://admin.sortviewapp.com` and, for the soak, the trial origin) and set `SORTVIEW_CUSTOMER_APP_URL` to
   `https://app.sortviewapp.com`. `SORTVIEW_ADMIN_ALLOWED_ORIGINS` was not changed. The backend redeployed healthy.
5. Added the Cloudflare CNAME from `app` to the distribution, DNS only.
6. Ran the smoke test on `https://app.sortviewapp.com`, checked admin, customer Streamlit and the collectors, and ran
   the client-IP check (section I). All passed (`docs/production-verification-runbook.md` Part 4).

### M.7 Verification

- The recorded production result is in [`production-verification-runbook.md`](production-verification-runbook.md),
  Part 4.
- After every later customer deployment, repeat at least: sign-in, refresh and sign-out; a direct load and refresh of a
  deep route (for example a sorter's reports); `/api/auth/session` answering 401 JSON when signed out; and a check that
  `admin.sortviewapp.com`, the customer Streamlit app while it remains, and the collectors are unaffected.
- Repeat section I if the distribution's `/api/*` behavior, its origin, or the backend's hosting changes.

### M.8 Rollback

| Problem | Do this |
| --- | --- |
| **Bad customer build** | Restore the prior `index.html` version (versioned bucket), or upload the saved `dist` of the previous commit with the commands in M.4. Invalidate `/` and `/index.html`. The previous hashed assets are still in the bucket (no `--delete`). |
| **The React customer app cannot be used** | Send customers back to the customer Streamlit app, <https://sortview.streamlit.app/>, which stays separately reachable until A5. Both operate on the same production data, so no data migration or data rollback is required. |
| **Allowed-origin or app-URL values wrong** | Restore the DigitalOcean values recorded before the change and let the backend redeploy. Streamlit and the collectors never read them. |
| **CloudFront behavior wrong** | Correct the behavior, origin or function association to M.2. Invalidate the affected paths if cached content is wrong. |

- The distribution, bucket and certificate can stay provisioned even if customer traffic is moved back to Streamlit.
- Once A5 retires the customer Streamlit app, rollback is this section's artifact, configuration and CloudFront steps
  only.
