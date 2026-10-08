# React hosting runbook: the CloudFront trial

How to host the React customer app (`frontend/`) behind Amazon CloudFront, with its `/api/*` requests sent
to the existing FastAPI service, for a trial on CloudFront's generated hostname.

> **THIS RUNBOOK DOES NOT CUT CUSTOMERS OVER FROM STREAMLIT.**
>
> The trial runs alongside the live customer dashboard, <https://sortview.streamlit.app/>, which stays the
> customer UI throughout. No customer is pointed at the trial. Moving customers is a separate, later step.

```
Browser ──► https://<DISTRIBUTION_DOMAIN>   (CloudFront, global)
              ├── default behavior  ──► private S3 bucket (the built React app)
              └── /api/*            ──► https://sortview-app-2p336.ondigitalocean.app (FastAPI, unchanged)
```

The browser only ever talks to the CloudFront hostname. The app requests `/api/...` on its own origin, so
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
- **No custom domain** is needed or used for this trial. The distribution's generated `*.cloudfront.net`
  hostname is the address.
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
- Alternate domain names: **none** (section K is future work). Certificate: the default CloudFront certificate.
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
  **STOP BEFORE R9G CUTOVER.**

**Why it matters.** The customer API's login and password limits (SlowAPI) key on `request.client.host`. If
that becomes a CloudFront address, many customers share one rate-limit identity: one person's failed
logins could lock everyone out for a minute. The fix would be a backend change to make the limiter
proxy-aware. Its design depends on what the logs show, so it is not specified here and must be built and
reviewed before any customer is moved.

## J. Rollback

| Problem | Do this |
| --- | --- |
| **Bad frontend build** | Restore the previous `index.html`: S3 → the object → Versions → restore the prior version, or `aws s3 cp` the saved `dist/index.html` of the previous commit with `--cache-control "no-cache"`. Invalidate `/index.html` and `/`. The previous hashed assets are still in the bucket (no `--delete`), so it works at once. |
| **`/api/*` behaving wrongly** (HTML instead of JSON, cookies missing, 403s) | Correct or revert the `/api/*` behavior's settings to section D. Check no custom error responses exist. Customers are unaffected: Streamlit is still the production UI. |
| **Login or origin configuration wrong** | Restore the two DigitalOcean values recorded in section F. Streamlit and the collectors never read them and are untouched. |
| **The trial itself** | Leave it unused while debugging, or disable the distribution. No customer address points at it, so nothing depends on it. |

## K. Future: a custom domain (NOT part of this trial)

> SortView does not currently own or use a custom domain such as sortview.com. Nothing in this trial
> needs one. This section is the outline for later, if a domain is wanted.

1. Register the domain, then choose where its DNS is hosted. Route 53 is not required; any provider works.
2. In **AWS Certificate Manager in us-east-1** (CloudFront only uses certificates from that region),
   request a certificate for the chosen hostname, and validate it by DNS: add the CNAME record ACM gives
   at the DNS provider.
3. Add the hostname to the distribution's **alternate domain names** and select the certificate.
4. At the DNS provider, point the hostname at `<DISTRIBUTION_DOMAIN>`: a CNAME, or an alias record if the
   DNS is in Route 53.
5. Update DigitalOcean: add `https://<hostname>` to `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS`, and set
   `SORTVIEW_CUSTOMER_APP_URL=https://<hostname>`. The session cookie is per hostname, so users sign in
   again on the new name.
6. Repeat section H on the new hostname, including section I.
