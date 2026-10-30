# release process

This document defines the standard release flow for SortView.

It covers:
- database migrations
- backend API deployment
- React admin app deployment
- React customer app deployment
- AMH agent validation and deployment
- preserving Collector and setup program artifacts in the private artifact repository

The goal is to make releases predictable, repeatable, and low risk.

---

## release order

When a release includes multiple types of changes, use this order:

1. database migration
2. backend API
3. React admin app
4. React customer app
5. AMH agent

This reduces the chance of schema mismatches, API payload mismatches, or stale frontend assumptions.

---

## before starting a release

Before deploying anything, confirm:

- changes are committed locally
- relevant tests have been run
- the target environment is correct
- the correct database credentials are available
- the change scope is understood:
  - backend only
  - frontend only (customer or admin app)
  - migration only
  - agent only
  - or some combination

If the release includes a schema change, review the Alembic migration before doing anything else.

### Collector releases: keeping tests from going stale

`collector.__version__` (`collector/__init__.py`) is the only place the Collector version is written. To release a
new Collector, bump it there by hand -- **no test file should need editing for a version bump**. Ordinary tests
compare against `collector.__version__` (or against what a component reports), never against a copied number.

Two checks enforce this, both fast, dependency-free and run by CI on every pull request (the `lint` job):

- `python scripts/check_test_freshness.py` -- flags a test that compares `collector.__version__` with a literal, asserts
  the current release as a literal, has a release number in its name, or treats a fixed timestamp as "now"
  (a `NOW`/`TODAY` constant) without the shared controlled clock. It also flags production code that hardcodes a
  Collector release as a default -- e.g. a "Collector version" form field, which must import
  `collector.__version__` instead. Historical version fixtures, other kinds of version (schema, API, agent) and
  explicit expiry/boundary timestamps are not flagged. A legitimate exception is annotated where it lives:
  `# freshness: allow FRESH004 -- <reason>` (the reason is required).
- `python scripts/check_release_readiness.py [--expect-version X.Y.Z]` -- also verifies the version authority is
  a single literal with no copies, the packaging/release tooling derives from it, and the time-sensitive enrollment
  tests are wall-clock-free. `--expect-version` is the one place to assert an exact release number: pass the version
  you are about to build.

`python -m collector.build_release` and `collector/freeze/build_frozen.ps1` run the readiness check before building
anything and refuse to continue if it fails.

Time-sensitive tests read one controlled clock (`tests/controlled_clock.py`), generate fixtures from it and reach
"expired" by advancing it -- never by picking a fixed "long ago" date -- so they cannot start failing as real time
passes.

### SortView setup program package

The repository's setup program (`installer/SortView.Setup`) always builds as one that cannot install anything:
`installer/installation-gate.props` stays `false` and `installer/bundle-pin.props` stays empty. The distributable,
installation-enabled package is made from a canonical Collector release by:

```
python scripts/package_installer_release.py --version <X.Y.Z> --bundle-zip <SortViewCollector-X.Y.Z.zip> --output <new folder>
```

1. Record the release first: `docs/releases/collector-<X.Y.Z>.md` must hold exactly one `- Release ZIP SHA-256:` and one
   ``- Bundle `MANIFEST.json` SHA-256:`` line, and be committed -- the script reads that record from `HEAD`, never the
   hashes from the bundle or the command line (`--release-record-file` reads an uncommitted record, and the package
   record then says so).
2. The script checks the ZIP and the bundle against those hashes and the bundle against its manifest, builds the setup
   program from a temporary `git archive HEAD installer` copy with the gate on and the pin set to the bundle, reads the
   built program back (gate Enabled, pin = the recorded hash and version, its own `BundleVerifier` accepts the packaged
   bundle, .NET Framework 4.7.2), and only then writes `<output>`: `SortView-Setup-<X.Y.Z>\` (the program, its three
   libraries and `bundle\`), its ZIP, `PACKAGE-SHA256.txt` and `PACKAGE-RECORD.json`.
3. It needs MSBuild (`--msbuild`, `SORTVIEW_MSBUILD` or vswhere) and the .NET Framework 4.7.2 reference assemblies:
   the targeting pack, or `--framework-root` / `TargetFrameworkRootPath` pointing at the
   `Microsoft.NETFramework.ReferenceAssemblies.net472` package's `build` folder.

The package is unsigned until code signing (C1) exists.

### Preserving release artifacts (private artifact repository)

Every canonical Collector release ZIP and every setup program package is kept in the **private** Git repository
`richardrhanly-us/sortview-artifacts` (local clone: `C:\Users\rhanly\Projects\sortview-artifacts`). **An artifact is
not preserved until** it has been inspected, copied, hash-verified, committed and pushed there, and its release record in
this repository names the artifact-repository path and commit. A ZIP that exists only in a build folder is not preserved:
that is how the canonical 1.0.14 bundle and the G2 setup package were lost (2026-10-28). Both were later recovered and
stored the same day (`docs/releases/collector-1.0.14.md`).

**Storage is not release approval.** An artifact in `sortview-artifacts` is internal. Whether it is published, or
approved for a customer, is recorded only in its release record in this repository (`docs/releases/`) -- never by where
it is stored. Historical release hashes in those records are never changed.

#### Layout

```
sortview-artifacts\
  collector\<version>\
    SortViewCollector-<version>.zip      the canonical release ZIP, unchanged
    ARTIFACT-RECORD.md
  installer\<version>\<build-id>\
    SortView-Setup-<version>.zip         the setup program package ZIP, unchanged
    PACKAGE-SHA256.txt                   as written by scripts/package_installer_release.py
    PACKAGE-RECORD.json                  as written by scripts/package_installer_release.py
    ARTIFACT-RECORD.md
```

- `<version>` is the Collector release version, e.g. `1.0.14`.
- `<build-id>` is `<YYYYMMDD>-<first 7 characters of the installer source commit>`, e.g. `20261029-041500e`. A second
  package from the same commit on the same day adds `-r2`, `-r3`, and so on.
- A folder is created once and never overwritten or reused. A Collector version has exactly one canonical ZIP: if
  `collector\<version>\` already holds a ZIP with a different SHA-256, **stop** -- two artifacts claim the same release.

#### `ARTIFACT-RECORD.md`

One per artifact folder, with every field filled in (write "unknown" rather than leave one out or guess):

| Field | Collector ZIP | Setup package |
|---|---|---|
| Artifact | file name | file name |
| SHA-256 | full 64 hex characters | full 64 hex characters (`zip_sha256` in `PACKAGE-RECORD.json`) |
| Size | bytes | bytes |
| Source commit | full 40 characters (the commit the bundle was built from) | full 40 characters (`installer_source` in `PACKAGE-RECORD.json`) |
| Embedded bundle | -- | release ZIP and `MANIFEST.json` SHA-256 (`release_zip_sha256`, `bundle_manifest_sha256`) |
| Original build path | full path of the ZIP as built | the `--output` folder as given |
| Build machine | `$env:COMPUTERNAME` of the machine that built it | same |
| Build date | UTC date and time | `created_utc` in `PACKAGE-RECORD.json` |
| Toolchain | CPython and PyInstaller versions | MSBuild path and reference assemblies (`PACKAGE-RECORD.json`) |
| Release record | `docs/releases/collector-<version>.md` | same |
| Validation status | what was validated, where and when -- or `not validated` | same |
| Inspection | the date, who inspected, and the result of the check below | same |
| Approval | `internal artifact -- not published, not customer-approved` unless the release record says otherwise | same |

#### Before copying: inspect for secrets and library data

Run this against the ZIP that is about to be stored. It reads the ZIP and changes nothing:

```powershell
$zip = '<full path of the ZIP>'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($zip)
try {
  $refused = '(^|/)pilot/|classification_rules\.json$|(^|/)collector_config\.json$|\.dpapi$|api_token|\.(pfx|key|env)$|(?<!/certifi/cacert)\.pem$|\.log$|(^|/)(data|logs|secrets|state)/|TLCFinalDlls|Checkins\.txt$|Rejects\.txt$|ACS Log\.txt$'
  $archive.Entries | ForEach-Object { $_.FullName.Replace('\', '/') } | Where-Object { $_ -match $refused }
  $archive.Entries | Where-Object { $_.Length -lt 1MB -and $_.FullName -match '\.(json|ps1|psm1|txt|md|ini|cfg|config|xml|py)$' } | ForEach-Object {
    $reader = New-Object IO.StreamReader($_.Open())
    try { if ($reader.ReadToEnd() -match '(?i)(bearer\s+[a-z0-9]|postgres(ql)?://|password\s*[:=]\s*\S|api[_-]?token\s*[:=]\s*\S)') { $_.FullName } }
    finally { $reader.Dispose() }
  }
} finally { $archive.Dispose() }
```

**Any output stops the procedure:** the artifact is not stored until the finding has been reviewed and explained in its
`ARTIFACT-RECORD.md`. Entries that are scripts or examples by design (for example `tools\set-api-token.ps1`, or
`collector_config.example.json`, which the patterns above do not match) are not findings. The one `.pem` file a
Collector bundle carries by design, `runtime\_internal\certifi\cacert.pem` (public certificate authorities), is
excluded above. Bundles 1.0.6 to 1.0.13 contain
`pilot\classification_rules.json`, generated from one library's settings, so they fail this check and are **not**
stored here. 1.0.14 and later bundles do not contain it.

#### Copy and verify (PowerShell, on the build machine)

```powershell
$source   = '<full path of the ZIP as built>'
$expected = '<full SHA-256 from the release record, or zip_sha256 from PACKAGE-RECORD.json>'
$folder   = 'C:\Users\rhanly\Projects\sortview-artifacts\collector\<version>'   # or installer\<version>\<build-id>

if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $expected.ToUpper()) { throw 'source does not match the recorded SHA-256' }
if (Test-Path -LiteralPath $folder) { throw "$folder already exists: never overwrite" }
New-Item -ItemType Directory -Path $folder | Out-Null
Copy-Item -LiteralPath $source -Destination $folder
$copy = Join-Path $folder (Split-Path -Leaf $source)
if ((Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash -ne $expected.ToUpper()) { Remove-Item -LiteralPath $folder -Recurse; throw 'the copy does not match: removed' }
if ((Get-Item -LiteralPath $copy).Length -ge 100MB) { throw 'GitHub refuses files of 100 MB or more: decide on Git LFS before storing' }
```

For a setup package, also copy `PACKAGE-SHA256.txt` and `PACKAGE-RECORD.json` from the `--output` folder into the same
folder. Then write `ARTIFACT-RECORD.md`.

#### Commit and push (the product owner does this)

```powershell
cd C:\Users\rhanly\Projects\sortview-artifacts
git pull --ff-only
git status -sb                                       # only the new folder may appear
git add -- '<collector\<version> or installer\<version>\<build-id>>'
git commit -m "Add <artifact file name> (SHA-256 <first 12 characters>, source <7-character commit>)"
git push
git rev-parse HEAD                                   # the artifact commit, for the release record
git status -sb                                       # must show the branch in sync with origin
```

Never force-push, amend a pushed commit, rewrite history or delete a stored artifact in this repository.

#### Record it in this repository

In `docs/releases/collector-<version>.md`, beside that artifact's existing hashes, add one line:

```
- Stored: `sortview-artifacts` `<path of the ZIP inside that repository>`, commit `<full 40-character artifact commit>`
```

The hashes in the release record stay the source of truth. The artifact repository is storage only.

#### Retrieve and verify on another machine

```powershell
# first time
git clone https://github.com/richardrhanly-us/sortview-artifacts.git C:\Users\rhanly\Projects\sortview-artifacts
# afterwards
git -C C:\Users\rhanly\Projects\sortview-artifacts pull --ff-only

$zip      = 'C:\Users\rhanly\Projects\sortview-artifacts\<path from the release record>'
$expected = '<full SHA-256 from the release record in this repository>'
git -C C:\Users\rhanly\Projects\sortview-artifacts merge-base --is-ancestor '<artifact commit from the release record>' HEAD
if ($LASTEXITCODE -ne 0) { throw 'this clone does not contain the recorded artifact commit' }
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $expected.ToUpper()) { throw 'hash mismatch: do not use this file' }
```

Compare against the hash in **this** repository's release record, not only the copy in `ARTIFACT-RECORD.md` beside the
file. A mismatch means the file must not be used.

---

## part 1: database migration

Use this section only if the release includes a schema change.

### step 1: confirm Alembic state

From the backend repo root:

    alembic current

Confirm the current revision matches expectations.

Run it with a database role that can read `alembic_version`, i.e. the role that applies migrations. The application's
runtime role (`sortview_app`, which the DigitalOcean backend connects as) is deliberately
given no access to `alembic_version` (`scripts/runtime_role_privileges.py`), and is refused with
`permission denied for table alembic_version`. Do not grant it access to make this check work. From the runtime
environment, `alembic heads` shows only which revision the deployed code expects.

### step 2: create migration if not already created

If the schema change has not yet been recorded:

    alembic revision -m "describe the schema change"

Edit the generated migration file in:

    alembic/versions/

Review both `upgrade()` and `downgrade()` carefully.

### step 3: apply migration

When ready:

    alembic upgrade head

### step 4: validate migration state

Run:

    alembic current

Confirm the target database is now at the expected revision.

### migration validation checklist

After migration:
- expected tables and columns exist
- app code still starts
- no missing-column or missing-table errors appear
- the backend can still connect to Neon
- existing data was preserved as expected

### migration rules

- do not run Alembic from the AMH machine
- do not manage schema changes in request handlers
- do not use ad hoc SQL edits in production as the normal process
- commit migration files to GitHub

---

## part 2: backend API deployment

Use this section when backend code changes are included.

### backend responsibilities

The backend is responsible for:
- receiving `/upload`
- receiving `/upload-pipeline-status`
- authenticating the AMH agent
- writing data into Neon

### deployment steps

1. push backend code changes to GitHub
2. deploy or restart the backend service
3. if the release includes schema changes, make sure Alembic migration has already been run
4. verify the backend starts cleanly

### backend validation checklist

After deployment, verify:
- API starts successfully
- the root health endpoint responds normally
- no startup tracebacks appear
- agent uploads do not return 500 errors
- `pipeline_status` rows continue updating
- recent checkins, rejects, and ACS rows continue appearing in Neon

### recommended post-deploy checks

Check:
- latest `checkins.event_time`
- latest `rejects.event_time`
- latest `acs_events.event_time`
- latest `pipeline_status.updated_at`

If these stop moving unexpectedly after deploy, pause and investigate.

---

## part 2b: React admin app deployment

Use this section when the admin app (`frontend/admin/`), or the customer-app modules it shares, change.

### admin app responsibilities

The admin app is responsible for platform administration at `https://admin.sortviewapp.com`:
- libraries and their overview
- diagnostics
- Collector configuration and Internal Routing
- installations and enrollment codes
- lifecycle
- provisioning

### deployment steps

1. deploy the backend first if the release adds or changes `/api/admin` routes the app calls
2. `npm ci` and `npm run build:admin` from a clean checkout of the release commit; keep `frontend/dist-admin`,
   named by commit, as the rollback artifact
3. upload `assets/` first, then `index.html`, and invalidate `/` and `/index.html`, exactly as in
   `docs/react-hosting-runbook.md`, section L.5

### admin app validation checklist

After deployment, verify the checks in `docs/deployment.md`, "admin app validation checklist". For a rollback, see
`docs/react-hosting-runbook.md`, section L.8.

---

## part 2c: React customer app deployment

Use this section when the customer app (`frontend/`, outside `frontend/admin/`) changes. It is the production customer
entry point at `https://app.sortviewapp.com` (A4, 2026-10-16). The full procedure is
`docs/react-hosting-runbook.md`, section M.

### customer app hosting

- `https://app.sortviewapp.com`: a Cloudflare CNAME, **DNS only**, to the customer CloudFront distribution, with the
  existing ACM certificate (which also covers `admin.sortviewapp.com`)
- the default behavior serves the private S3 bucket (OAC) through the SPA routing function; `/api/*` goes to the
  DigitalOcean backend, uncached

### deployment steps

1. deploy the backend first if the release adds or changes `/api` routes the app calls
2. from `frontend/`, on a clean checkout of the release commit: `npm ci`, then `npm run build`; keep `frontend/dist`,
   named by commit, as the rollback artifact
3. boundary check: the customer `dist/` must not contain `/api/admin` or "SortView Administration" (section M.4)
4. upload `assets/` first with `--cache-control "public,max-age=31536000,immutable"`, then `index.html` with
   `--cache-control "no-cache"`; never use `--delete`
5. invalidate `/` and `/index.html` on the customer distribution

### backend environment

- `https://app.sortviewapp.com` must be in `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS`
- `SORTVIEW_CUSTOMER_APP_URL` must be `https://app.sortviewapp.com`
- `https://admin.sortviewapp.com` stays in `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` (admin sign-in and sign-out) as well as
  in `SORTVIEW_ADMIN_ALLOWED_ORIGINS`
- record the current values privately before any change; saving them redeploys the backend

### customer app validation checklist

After deployment:
- the customer smoke test (`docs/react-hosting-runbook.md`, section M.7): sign-in, refresh, sign-out, a deep route
  loaded directly and refreshed, and the pages the release touched
- client-IP preservation (`docs/react-hosting-runbook.md`, section I) if the distribution's `/api/*` behavior, its
  origin or the backend's hosting changed
- `https://admin.sortviewapp.com` unaffected
- collectors unaffected

### rollback

See `docs/react-hosting-runbook.md`, section M.8: the previous known-good build (S3 object version or saved `dist`),
the CloudFront configuration, the DigitalOcean values or the backend's previous commit. There is no other customer UI
to fall back to; the customer Streamlit app was retired in A5.

---

## part 4: AMH agent deployment

**This section describes the CURRENTLY-DEPLOYED production agent only** --
the archived snapshot under
`agent/SortViewAgent - What is currently sitting on the AMH computer/`,
not the top-level `agent/` package. A new canonical continuous agent
(`agent/main.py` + `agent/runtime/*`) has been built and tested in this
repo but has **not been deployed to or validated on the real AMH
machine** -- see `agent/README.md` for its status and remaining gates.
Do not use this section to deploy that new agent; that is a separate,
later, explicit cutover decision.

Use this section when changes to the *currently deployed* agent logic
are included.

### important boundary

The archived snapshot folder above is the source-of-truth reference for
what is actually running on the AMH machine today -- it is preserved
read-only in the repo specifically so there's never ambiguity about what
production behavior looked like. `agent/run_pipeline.py` (top-level, no
archive path) is a separate, legacy/validation-only local mirror kept for
side-by-side comparison during later validation; it is not what's
deployed.

The live AMH agent runs on the AMH-attached Windows machine and is manually updated from the approved archived-snapshot copy.

### deployment steps

1. validate source changes against the archived snapshot folder
2. copy the approved agent folder to the deployed AMH install root,
   keeping it as an `agent/` subfolder there (not flattened) -- every
   module uses package-relative imports, so it only runs as a package
3. confirm `agent_config.json` is still correct
4. open Command Prompt on the AMH machine, in the install root (the
   parent of the deployed `agent/` folder -- not inside `agent/` itself)
5. run:

    python -m agent.run_pipeline

6. review output carefully
7. if healthy, return the scheduled task or normal execution process to service

### expected healthy output

A healthy validation run usually includes:
- pipeline run started
- start status upload returned 200
- parsing completed without exceptions
- upload batch returned 200
- completed status upload returned 200
- pipeline state file updated
- pipeline run completed successfully

### agent validation checklist

After deployment, verify:
- parser runs complete without exceptions
- offsets update correctly
- upload returns HTTP 200
- completed status returns HTTP 200
- new rows appear in Neon
- the customer app's Live Today reflects the new data

### agent deployment rules

- do not run Alembic from the AMH machine
- if the deployed AMH copy and the archived snapshot folder in the repo
  ever diverge, treat the archived snapshot as the version to reconcile
  against
- do not edit the archived snapshot folder directly -- it is preserved
  read-only historical evidence, not a working copy (see its own README)
- do not commit runtime output files as part of normal code release workflow

---

## full release checklist

Use this when a release affects more than one layer.

### pre-release
- confirm scope of change
- confirm target environment
- confirm working tree is clean or intentionally staged
- confirm migration needs
- confirm tests were run

### migration
- run `alembic current`
- apply `alembic upgrade head` if needed
- verify migration result

### backend
- deploy backend
- verify API health
- verify upload behavior

### admin app
- build and upload `dist-admin` if included (assets first, `index.html` last, then invalidate)
- verify sign-in, a deep route and the origin guard

### customer app
- build and upload `dist` if included (assets first, `index.html` last, then invalidate)
- verify sign-in, a deep route, Live Today data and the latest pipeline status

### agent
- deploy agent changes if included
- run `python -m agent.run_pipeline` from the AMH install root (not from inside `agent/`)
- verify successful upload and status writes

### post-release
- confirm recent DB rows are moving
- confirm the customer app reflects current DB state
- confirm no unexpected errors in logs
- document any issues or rollback actions taken

---

## rollback mindset

Not every release needs a full rollback plan, but every release should consider failure boundaries.

Examples:
- migration failure
- backend starts but upload fails
- customer app loads but shows stale data
- AMH agent runs but payload no longer matches backend expectations

When something goes wrong:
1. stop making additional changes
2. identify which layer failed
3. restore last known-good code if needed
4. verify database state before applying more fixes
5. retest in the standard release order

---

## operational reminders

- Neon is the system-of-record database
- Alembic is now the official schema-tracking path
- `init_db.py` has been retired; the Alembic baseline migration is the schema evolution process
- the AMH agent is deployed manually even though its source-of-truth lives in the repo
- future improvements can automate parts of this process, but the documented order should remain the same
