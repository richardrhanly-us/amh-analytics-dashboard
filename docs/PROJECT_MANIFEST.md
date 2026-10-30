# SortView project manifest

The big picture, kept in one place so a single task does not swallow the plan. Read this before starting any
non-trivial task, and check the task against it before widening scope.

Last reviewed: 2026-10-28. Facts marked **(verify)** were not confirmed against production or the person who
knows; everything else was checked in the repository on that date.

---

## How to use this file

1. **Before a task:** name which workstream and which milestone it serves (section 4). If it serves none, it belongs
   in the Parked list unless changing the workstream order is a deliberate decision.
2. **During a task — the drift rule:** a new finding that is not needed to finish the current task goes in
   **Parked** (section 7) with one line and a date, and work continues. Stop and raise it instead only if it is:
   - a production, safety, PII/patron-privacy, or security risk, or
   - a genuine blocker for the current task.
3. **After a task:** tick what it finished in section 5, and update "Current focus" if the milestone moved.
4. **Changing the order** of the workstreams is a deliberate decision, recorded in the change log at the end —
   never something a task does on its way past.

Standing rules (no commits by Claude, city machines never touch the DB directly, PII care, lightest fix, the UI
playground never breaks production) live in `CLAUDE.md` and are not repeated here.

---

## 1. North star

A second library — including a **city/government** library — can be sold SortView, install the Collector on its
own AMH machine without us on site, sign in to a hosted customer app that passes a government IT review, and see
its own data. New Braunfels Public Library (NBPL) stays live throughout.

---

## 2. Priority order, and why

**Current order (revised 2026-10-19, product owner decision; change log).** Workstream A is complete. Workstream B is
**paused at Section 508 §602.3 (Electronic Support Documentation)** — an intentional pause, not a completed milestone
(section 4, B). Planned installer changes and visual/UX changes to the customer and admin apps would make customer
installation documentation, its accessibility verification and the VPAT out of date, so they come first:

| Priority | Work | Notes |
|---|---|---|
| 1 | **Installer improvements** | Review and improve the customer-facing installation, configuration, upgrade and recovery experience. Scope and implementation plan **not yet defined**: the next step is discovery/planning, not an approved specification. Relation to the C milestones is to be decided in that planning. |
| 2 | **UI redesign and polish** | Professional appearance, consistency, usability and visual presentation of the customer and admin apps. Designs and scope **not yet defined**. Accessibility stays an acceptance requirement for every changed component and workflow. |
| 3 | **Targeted accessibility regression verification** | After 1 and 2, verify affected functionality against the 2026-10-19 baseline. Reuse existing evidence for unchanged functionality where appropriate; do not automatically restart the whole B2 audit. |
| 4 | **Resume government readiness (B)** | Once the product changes stabilize; the resume list is in section 4, B. |

The original order below (set 2026-10-14) is kept for its reasoning; its "do A first" logic is what now puts the
installer and UI changes before the remaining B paperwork.

| Order | Workstream | Why it goes here |
|---|---|---|
| 0 | **Land in-flight work** | Finished branches that are not merged are the main source of drift. Close them first. |
| 1 | **A. Streamlit → React cutover** | Government readiness and customer onboarding both target *the customer UI*. When this order was set, that was Streamlit Community Cloud; since A4 (2026-10-16) it is React. Anything done to the UI surface before the cutover is done twice. |
| 2 | **B. Government readiness** | Evidence (accessibility, hosting, security, privacy) must describe the stack customers will actually use — so it follows A. Paperwork about data flows that are already stable (Contract v2) can start early (section 6). |
| 3 | **C. Installer commercialization** | The installer's own packaging is independent, but the *onboarding around it* (first account, where the customer lands, the guide, what "pending" looks like) lands on the React app and must meet B's requirements (signing, least privilege, no raw PII). Built last, it is built once. |

**What would force a backtrack if done out of order:**

- Installer onboarding before A: the install guide, first-login steps and "waiting for first data" screens would
  point at Streamlit and have to be redone.
- Government evidence before A: an accessibility conformance report, security headers, session handling and the
  hosting description written for Streamlit Community Cloud would be thrown away at cutover.
- Installer before B's collector requirements are settled: signing and privilege decisions retrofitted into an
  installer customers already have.

**What is safe in parallel** (does not move the plan, only shortens it): the long-lead items in section 6.

---

## 3. Current focus

**Workstream A is complete (A5 completed 2026-10-17). Workstream B is paused at Section 508 §602.3 (2026-10-19)
after the B2 accessibility baseline. Now: Priority 1, installer commercialization -- the SortView Setup GUI installer
and its supervised-pilot package for Collector 1.0.14, first-owner activation (C3) and customer onboarding (C4); status
and open items in section 4, C.** Then UI redesign and polish, targeted accessibility regression
verification, and resuming B. The VPAT and support-guide drafts stay internal until B resumes and completes.

Workstream A history:
- A1 and A1b completed 2026-10-14.
- A2 complete: items 1–7 merged (item 7: PR #210, `64c7d7b`); item 8, Super Admin Streamlit retirement, merged
  (PR #212, `88f2b5a`).
- A3 completed 2026-10-15: React admin is in production at `https://admin.sortviewapp.com`.
- **A4 completed 2026-10-16:** the NBPL customer React cutover. `https://app.sortviewapp.com` is the production
  customer entry point, and NBPL uses it (`docs/production-verification-runbook.md` Part 4).
- **A5 completed 2026-10-17:** the customer Streamlit app is retired -- hosted app deleted, source, tests, tooling and
  dependencies removed (PRs #215-#217), and its stale production data cleaned up (section 4, A5).

**Current production architecture:**

- Customer UI: the React customer app at `https://app.sortviewapp.com` -- the only customer UI.
- Platform administration: the React admin app at `https://admin.sortviewapp.com`.
- Both are static apps in private S3 buckets behind their own CloudFront distributions (Cloudflare DNS, ACM
  certificate), each with `/api/*` sent to the backend.
- Backend: FastAPI on DigitalOcean, installed from `requirements.txt`. Database: Neon PostgreSQL. Password-reset email
  over SMTP (Amazon SES).
- Ingestion: the SortView Collector (and the legacy agent) on each library's AMH machine, through the API only.
- `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` must hold `https://app.sortviewapp.com`, `https://admin.sortviewapp.com` and the
  customer distribution's generated hostname `https://d22i2vjcxygt0c.cloudfront.net` (`docs/react-hosting-runbook.md`, M.5).

Phase 0 (completed 2026-10-14):

- [x] `fix/v1-first-ingest-readiness` — legacy uploads make a pending branch ready. Merged (PR #190).
- [x] React "Waiting for first data" UI — merged (PR #191).

---

## 4. Workstreams

### A. Streamlit → React cutover

**Goal:** every customer uses the React app on a SortView domain; the Streamlit customer dashboard is retired.
The migration also includes the internal provisioning and library-management surface SortView needs to operate
(formerly the Super Admin Streamlit app, whose scope A1b set). React admin is that replacement; the Super Admin code is
removed by A2 item 8.

**Where it stands (verified):**

- Production customer UI: the React customer app at `https://app.sortviewapp.com` (A4, 2026-10-16), used by NBPL.
- The customer Streamlit app, `https://sortview.streamlit.app/`, was retired in A5 (2026-10-17): the hosted app is
  deleted and its code, tests, tooling and dependencies are removed. It is not a fallback.
- React customer app (`frontend/`) is already substantial and functional: Organizations, Organization page,
  Organization Reports (operations, value), Members, Account (including password reset), Settings (general,
  branches, routing, efficiency), Sorter Live Today, Sorter Reports (operations, efficiency). Keyboard-relevant
  accessibility semantics and focus behavior are covered by automated frontend tests; real screen-reader
  verification is documented separately in `docs/accessibility-verification-2026-10-19.md`.
- The React customer app is hosted through CloudFront on the customer distribution that previously served the trial,
  now with the alternate domain `app.sortviewapp.com` (`docs/react-hosting-runbook.md` section M).
- The React **admin** app is in production at `https://admin.sortviewapp.com` (A3, 2026-10-15; hosting runbook
  section L). The hosted Super Admin Streamlit app, its temporary fallback, was deleted on 2026-10-15 (A2 item 8).
- Recent "decoupling" slices extracted shared policy out of Streamlit (pipeline rules, access mode, settings
  merge, readiness, onboarding status).

**Milestones:**

- [x] **A1. Parity checklist.** **Completed 2026-10-14:** D1–D9 decided and recorded below; none adds customer
      React feature work to A2. A written list of every customer-facing Streamlit capability, each marked
      *in React* / *needs building* / *dropped on purpose*. Known candidates to decide:
  - Director PDF export (Streamlit `reports_labor.py`) — **decided 2026-10-14 (D1): dropped for cutover.** React
    Print report + CSV are the supported export story; the server-side PDF is not rebuilt. A single printable React
    summary view is parked until a customer or director asks for it.
    - **AMH ROI framing (product decision, not an implementation detail):** the legacy AMH "hours saved",
      annualized ROI, payback and since-install value are removed on purpose. Why: the "AMH rate" was observed
      demand, not machine throughput; arbitrary ranges were annualized; since-install projected the current range
      over unobserved years; the PDF carried NBPL-specific routing and unsupported narrative. Accepted framing
      (`services/efficiency_report.py`): manual-workload and labor-value *equivalents* from stored assumptions,
      unavailable when an assumption is missing, and no claim that staff hours, payroll or budget were saved.
    - Not affected: SortView Value's own ROI/payback (`services/sortview_value.py`), a separate model of the
      SortView subscription from customer-entered before/after task times, with its existing caveats.
  - Internal-routing lists (Branch/Collection Services names, patron account names) — **decided 2026-10-14 (D6):**
    still necessary: they decide which holds are subtracted from Public Holds (v1 on the server, v2 on the
    collector), even though D3 hides the service counts. Not added to ordinary customer React settings. Long-term
    owner: the React SortView administration surface (scoped by A1b), which edits them since A2 item 4; the Super
    Admin Streamlit path that collected them at provisioning is removed by A2 item 8. Known limit: hosted edits do not update an installed v2 collector's
    `classification_rules.json` (delivered once, at enrollment) — separate collector work under C6.
  - Streamlit admin-password lock (`security` settings) — **decided 2026-10-14 (D2): not rebuilt in React.** React
    relies on the API's server-side owner/admin role checks on every settings and members request. The Streamlit
    lock stays until cutover/retirement. Why: it guards only Admin Settings (not members), is a page-level gate
    rather than the authorization boundary, has no attempt limit or audit trail, and is a shared organization
    secret rather than per-user access. D2 does not close B3: MFA/SSO remains the stronger future control, and is
    not a cutover blocker unless new evidence says otherwise.
  - Streamlit Transits view and Overview (internal workflow) — **resolved 2026-10-14:** internal workflow / Holds by
    D3, Transits and Exceptions / Overflow by D5 (details below).
    - **Internal workflow decided 2026-10-14 (D3):** keep React's ranged Public Holds + ILL report as it is. Not in
      cutover scope: Branch Services / Collection Services counts, Live Today internal-workflow cards, the ILL split
      by destination branch. Why: Holds + ILL generalize to any library; the service counts depend on per-library
      account lists and NBPL terms (unconfigured lists read as false zeros), carry a small privacy edge when a list
      is one person's card, and are deliberately omitted by the tested API; a "today" range in Reports covers most
      of the live cards' value. Reopen only if NBPL staff confirm active operational use of the service counts —
      and then evaluate the ranged report first, not Live Today.
    - **Transits deep reports and Exceptions / Overflow decided 2026-10-14 (D5): dropped; nothing to build in A2.**
      React's routing and reject reporting already covers the general questions: settings-driven destinations,
      where check-ins went, daily/monthly transit (with a per-destination CSV), previous-period comparison, the
      organization routing network, reject reasons, bin volume, CSV and print. Not carried forward: transit by
      hour, % routing over time, peak transit weekday, all-history destination-share baselines, the "No Agency
      Destination" count, deep dive and item-level title/barcode table, destination-linked reject diagnostics, and
      the bin-0 "exception/overflow" figures including "Estimated Holds". Why: NBPL-specific (Westside / Library
      Express / "No Agency Destination" / bin 0), inferred or weak logic, redundant with React, or raw item rows
      outside the v2 privacy boundary; real hold counts are kept under D3, and diagnostic text was dropped in D4.
  - Automatic alerts / insight text — **decided 2026-10-14 (D4): metrics only at cutover.** React shows the
    underlying figures with no automatic diagnosis or advice; none of Streamlit's alert/insight logic is ported
    (routing trend alerts, "Recommended Attention", transit/reject correlation, reject-rate colour bands, the unused
    "critical" alerts, other advisory text). Why: hard-coded thresholds from one library, NBPL-specific wording that
    mislabels another library's destinations, advice not validated enough to recommend action, and React's tested
    "show the data, do not diagnose or advise" boundary (`frontend/src/test/scope.test.ts`).
  - Demo/guest mode — **decided 2026-10-14 (D7): dropped.** No anonymous or shared-credential guest access in
    React, and Streamlit is not kept alive after cutover for a demo. If a sales/evaluation demo is needed later, it
    uses a dedicated isolated demo organization with synthetic data (never copied from a customer) and a normal
    authenticated account with the minimum role, under normal React/API auth. Not an A2 requirement; its
    provisioning may be considered in A1b or later demo tooling.
    - **Production security finding during D7 (not a feature):** the README links to
      `https://sortview.streamlit.app/?guest=1`, NBPL's production app. Read-only checks found the historical
      `guest@gmail.com` account active, not a platform admin, with an NBPL `viewer` membership that reached NBPL's
      operational data, and `last_password_changed_at` NULL; production Streamlit Secrets held the historical
      guest email and a weak, historical shared password. So NBPL viewer data was exposed whenever guest auto-login
      was on, and a weak shared login was usable through the normal sign-in.
    - **Remediated by the product owner:** `SORTVIEW_DEMO_MODE_ENABLED` set to false in production Streamlit; the
      NBPL membership removed through the supported app path (verified read-only: `removed_at =
      2026-10-15 00:49:21.772118+00`), so the account has no NBPL access. The account itself was not deleted or
      deactivated (still active at the last check). Remaining cleanup is under A5.
  - Leftover Admin Settings fields — **decided 2026-10-14 (D8): React customer settings are sufficient; no D8 work
    in A2.** Already covered: organization name, status and role (General), branch/sorter inventory (Branches),
    routing (Routing). Moved to A1b's admin scope, not customer settings: renaming the real organization, branch or
    sorter records; system/vendor name; support contact name/email; internal support notes; plan/subscription
    management. Dropped: the `library_name` and `branch_name` display copies, `account_settings.organization_name`,
    the free-text `account_settings.plan_name`, the "Current DB Preview", and the raw readiness code. Why:
    duplicate names are multiple sources of truth, the free-text plan conflicts with the real subscription, the
    preview is a raw-settings debug surface, and React already shows readiness in plain language. React does not
    reproduce Streamlit's exposure of provisioning notes and support contacts to customer admins.
    Customer-visible plan/subscription display is deferred to C7.
  - Pipeline / collector diagnostics — **decided 2026-10-14 (D9): React's current status is sufficient; no D9 work
    in A2.** React already answers "is my sorter sending data?" with the pipeline state (OK / Degraded / Failed /
    Unknown), last reported time, today's figures and hourly activity, refresh status, the waiting-for-first-data
    state and sorter status labels. No separate "last successful upload" line. Moved to A1b's admin/support scope:
    last attempt vs. last success, raw status codes, heartbeat detail, `last_error_class`, pending outbox and
    quarantined counts, oldest pending event, collector schedule / next run / duration, v1 per-run counters,
    status-row timestamps, ingest-key/authentication troubleshooting. Dropped: the schema-validation message that
    shows table/column names, the v1 run destination breakdown, and other raw detail customers cannot act on.
  - History window — verified 2026-10-14 in NBPL production: plan `enterprise` (one subscription),
    `history_days` enabled with 3650, so React/API give NBPL a 3650-day window. No 30-day fallback risk at NBPL
    cutover. The general rule stands for other customers: a missing or disabled `history_days` falls back to
    30 days (`entitlement_service.DEFAULT_HISTORY_DAYS`); Streamlit never enforced it.
- [x] **A1b. Super Admin parity audit** (read-only; immediately after D9, before any A2 work). Inventory every
      Super Admin capability needed to provision and manage libraries, classify each as migrate / drop /
      redesign, and set the React administration scope. **Completed 2026-10-14** (audit plus follow-up, including
      `docs/SortView_Internal_Customer_Provisioning_Checklist.docx`, which added no new capability).
  - Must also decide (from D9): the customer-accessible `GET …/ingest-status` route returns the full v2 heartbeat to
    any organization member, and React deliberately never calls it. Move it behind internal/admin/support
    authorization, or retire it. **Decided: move it into the admin diagnostics route and remove customer access
    (A2 item 3).**
  - **Decisions (2026-10-14):**
    1. *Admin UI:* a separate React admin frontend/origin, not `/admin` inside the customer bundle — privileged
       code stays out of the customer bundle, the customer scope tests stand, and authorization sits behind a
       separate `/api/admin` server boundary.
    2. *Internal Routing:* platform admins may view and edit the entries in React admin. Platform-admin only;
       never returned through customer APIs; never in logs or audit text (audit counts/metadata only). Replaces
       the customer-Streamlit-only editing path.
    3. *A4 sequencing:* A2 items 1–4 are required before A4, so Internal Routing editing and v2 diagnostics never
       disappear.
    4. *`system_name`:* dropped, not migrated. Vendor/equipment metadata, if ever needed, is modelled deliberately.
    5. *Multi-branch:* non-primary-branch / multi-site provisioning is out of Workstream A and assigned to C (C8).
  - **Required before Super Admin Streamlit retires:** admin API/auth foundation; v2-aware libraries list and
    detail; provisioning; operational identity assign/retry; installations add/edit/retire; enrollment code
    generation; collector site settings; Internal Routing editing; lifecycle suspend/reactivate/offboard; v1/v2
    diagnostics; customer `ingest-status` moved behind admin/support access.
  - **Deferred:** first owner → C3; legacy agent config / installer values → C5/C2; replacement machine, key
    rotation, installed-rule refresh → C6; plans, entitlements, subscription management → C7; multi-branch → C8;
    platform-admin management and MFA → B3; demo tenant → future C / sales tooling.
  - **Dropped:** Super Admin home stubs; raw `st.json` dumps; dead provisioning keys (`security`, `library_name`,
    `account_settings.organization_name`, `account_settings.plan_name`); `system_name`.
  - **Operator-only, outside React:** tenant purge (`scripts/purge_tenant_data.py`); uptime and pipeline
    monitoring scripts; legacy credential scripts while still needed, until C removes them.
- [x] **A2. Close parity gaps** decided in A1 and A1b (each its own small PR). **Completed:** items 1–7 merged and
      smoke-tested in production (2026-10-15); item 8 merged (PR #212, `88f2b5a`). No customer React feature work came
      out of A1; the backlog was the React admin, in this order:
  1. Admin API foundation + separate React admin shell.
  2. Libraries list + library overview, v2-aware.
  3. Admin diagnostics + move/remove customer `ingest-status`.
  4. Collector site settings + Internal Routing editor. **Implemented 2026-10-15**, merged
     (`feature/a2-admin-collector-config`): library-level admin routes and React page for the Collector
     time zone + review record and the four Internal Routing lists; transit shown read-only (customers keep editing it
     in React Settings › Routing); audit counts only; customer Streamlit no longer shows or saves Internal Routing.
     Saved to SortView only -- installed Contract v2 collectors are not updated (C6).
  5. Installations + enrollment code. **Implemented 2026-10-15**, merged (PR #208, `eb3b304`): library-level admin
     routes and React page to list every installation, add one at the active primary branch (Contract 2 by default, needing item 4's settings; Contract 1 still offered), rename, deactivate, return to
     provisioning, retire (final in React), and issue a one-time enrollment code shown once. A Contract 2 code is
     refused for a branch already enrolled (`branch_already_enrolled`); replacement is C6. No hostname, Collector
     version or legacy Installer Values (C5/C2); no branch choice (C8). Audit ids and counts only, never a code.
  6. Library lifecycle (suspend/reactivate/offboard). **Implemented 2026-10-15**, merged (PR #209, `7f3fd6d`): three explicit admin actions (`POST …/lifecycle/suspend|reactivate|offboard`, no generic status
     write) on the two existing `platform_admin_service` functions, and a React page `/libraries/:orgSlug/lifecycle`
     linked from the overview. Semantics unchanged: suspension writes only the status (Collector refused, members keep
     read-only access); reactivation always returns to `active`; offboarding is the existing one-transaction cutoff and
     is refused (409) for an already-cancelled library. Each change writes `admin_library_suspended` /
     `_reactivated` / `_offboarded` to `auth_audit_log` in the same transaction (status names and counts only); the
     offboarding's lifecycle evidence names the administrator as `platform_admin:<id>`, never by e-mail. No purge, no
     un-offboard.
  7. Provision library + assign operational identity. **Implemented 2026-10-15**, merged (PR #210, `64c7d7b`; the
     commit DigitalOcean runs): `POST /api/admin/libraries` and React `/provision` (linked from Libraries) create, in ONE
     transaction, the organization (active), its one active primary branch, a trial subscription on an explicitly
     chosen active plan (`GET /api/admin/plans`), the existing empty settings rows, the operational identity and an
     `admin_library_provisioned` audit row, through the existing `tenant_service` functions on one connection (Super
     Admin's non-atomic path is unchanged until item 8). Names trimmed, 1–120; slugs 1–63 in
     `^[a-z0-9]+(?:-[a-z0-9]+)*$`, never rewritten by the server; a taken slug is 409 `library_slug_taken`. No user,
     membership, installation, code, token, key, cutover, Collector setting, contact, note or legacy settings copy; the
     overview then lists the next steps (items 4 and 5) and states that no one can sign in until a first owner exists
     (C3). `POST /api/admin/libraries/{slug}/operational-identity` and an overview button replace Super Admin's assign /
     retry for a library whose identity is missing or partial; an inconsistent mapping is refused, never rebuilt. No
     numeric id is shown, returned or audited.
  8. Retire Super Admin Streamlit, after 1–7 are proven in production. **Unblocked 2026-10-15**
     (`docs/production-verification-runbook.md` Part 3). The item 8 audit's only blocker was that React admin had not
     been production-hosted or proven. React admin is now production-hosted (A3) and items 1–7 passed a production
     smoke test. The retirement sequence still stands:
     1. React admin proven in production. **Done.**
     2. Items 1–7 smoke-tested in production. **Done.**
     3. Keep the hosted Super Admin app as the temporary fallback until the retirement sequence runs. **Done.**
     4. Delete or disable the hosted Super Admin app on Streamlit Community Cloud **before** merging the source
        deletion. Otherwise Community Cloud redeploys from `main` into a broken app. **Done 2026-10-15:** deleted; the
        old URL no longer serves the app, and customer Streamlit still works.
     5. Then implement and merge item 8's source, test, docs and CI cleanup. **Merged** (PR #212, `88f2b5a`):
        `super_admin/` and `src/pages/Super_Admin_Home.py`
        deleted with their UI-only tests; still-valid service tests moved to `tests/test_tenant_service.py` and
        `tests/test_operational_identity.py`; guards, CI (bandit path) and current docs updated. Shared services, the
        admin API, React admin, customer Streamlit, operator scripts and Collector code are unchanged. Item 8, and
        with it A2, is complete.

     First-owner onboarding stays C3, unchanged by item 8. Legacy installer values / `agent_config.json` stay C5/C2,
     replacement-machine work C6, plans C7, multi-branch C8; customer Streamlit retirement is A5.

  A4 critical path: 1 → 2 → 3 → 4.
- [x] **A3. Production hosting.** **Completed 2026-10-15** for the admin app; the customer production hostname is
      locked and its move is A4.
  - **Admin:** in production at `https://admin.sortviewapp.com`, on its own CloudFront distribution (free flat-rate
    plan) and its own private, versioned S3 bucket.
    - `/api/*` goes to the DigitalOcean backend (HTTPS only, TLS 1.2+, caching disabled, all viewer data except Host);
      the default behavior goes to S3 with the `sortview-spa-routing` function on viewer request.
    - TLS is an ACM us-east-1 certificate (Issued) covering `admin.sortviewapp.com` and `app.sortviewapp.com`.
      Cloudflare DNS holds a DNS-only CNAME to the distribution, plus the validation CNAMEs.
    - DigitalOcean lists the admin origin in **both** `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` (sign-in and sign-out) and
      `SORTVIEW_ADMIN_ALLOWED_ORIGINS` (admin writes).
    - Deployment is manual: `docs/react-hosting-runbook.md` section L, with the evidence in
      `docs/production-verification-runbook.md` Part 3.
  - **Locked (2026-10-15):** admin production is `https://admin.sortviewapp.com`, and customer React production is
    `https://app.sortviewapp.com` (in production since A4, 2026-10-16). They are separate CloudFront distributions
    and hosting surfaces, because the admin write guard requires an origin of its own.
  - **Not done in A3:** no customer was moved; at that point the customer React trial stayed on its generated CloudFront
    hostname and Streamlit stayed the customer UI. The customer move was A4.
- [x] **A4. Cutover:** NBPL moved to React. **Completed 2026-10-16** (`docs/react-hosting-runbook.md` section M;
      `docs/production-verification-runbook.md` Part 4).
  - The existing customer CloudFront distribution and its private S3 bucket (OAC) were reused. The existing ACM
    certificate (covering `app.sortviewapp.com` and `admin.sortviewapp.com`) was attached with the alternate domain
    `app.sortviewapp.com`, and Cloudflare holds a DNS-only CNAME to the distribution.
  - DigitalOcean: `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` includes `https://app.sortviewapp.com`, still includes
    `https://admin.sortviewapp.com` (admin sign-in and sign-out use that guard), and temporarily keeps the trial
    CloudFront origin for the soak. `SORTVIEW_CUSTOMER_APP_URL` is `https://app.sortviewapp.com`.
    `SORTVIEW_ADMIN_ALLOWED_ORIGINS` is unchanged (`https://admin.sortviewapp.com`). The backend redeployed healthy.
  - The customer bundle was built from `main` at `2a8e40f` and passed the boundary check. The production smoke test
    passed; admin, customer Streamlit and the collectors stayed healthy; and the client-IP check passed on the
    customer path. No customer data migration was needed.
  - The customer Streamlit app remains live as a temporary rollback target. Removing customer access to it is A5.
- [x] **A5. Retire** Streamlit customer code paths and the customer Streamlit deployment. **Completed 2026-10-17**
      (`docs/production-verification-runbook.md` Part 5).
  - Production inventory (read-only): no active admin-lock credential anywhere; four `organization_settings` rows held
    an obsolete `security` block (nbpl, sortview-clean-install-test, test-install, rehearsal-library);
    `branch_settings` held none.
  - Legacy guest cleanup (D7): the historical guest account was deactivated through the application's own account
    service (sessions revoked, audit row written); the README demo link and the guest/demo settings are gone.
  - The hosted customer Streamlit app was deleted from Streamlit Community Cloud. Its database credential was the
    backend's shared runtime role (`sortview_app`), so it was kept, not revoked.
  - Customer Streamlit source, views, services, adapters and Streamlit-only tests removed (PR #215); the remaining
    Streamlit-only shared code and tooling -- `.streamlit/config.toml`, the cookie probe, the redaction canary, the
    admin-lock service and its migration tooling -- removed, `database.py` reads `DATABASE_URL` from the environment
    only, and password-reset email always uses the React reset URL (PR #216).
  - `streamlit`, `altair` and `weasyprint` removed from `requirements.txt`, and `packages.txt` deleted; a fresh
    production-only install from `requirements.txt` was verified (PR #217).
  - Admin-lock cleanup (D2): the obsolete `security` block was removed from exactly those four rows in one guarded
    transaction; every other setting and `branch_settings` were verified unchanged, and the customer app passed its
    smoke check afterwards.
  - **Not removed: the customer distribution's generated hostname.** Removing `https://d22i2vjcxygt0c.cloudfront.net` from
    `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` after the A4 soak was tried in production: customer writes from
    `https://app.sortviewapp.com` failed with "Request origin is not allowed.", and restoring it immediately restored
    normal behavior. It must stay; the cause is not yet explained.


**Exit:** no customer signs in to Streamlit, and the provisioning/library-management functions A1b finds necessary
have a supported React replacement, so the Super Admin Streamlit app can be retired (A2 item 8).

### B. Government readiness

**Goal:** SortView can pass a city/government IT, security, privacy and accessibility review, with written
evidence for each answer. The original "government-readiness audit" (Parts 1–7, referenced in code comments)
covered Contract v2 dashboard wiring, mixed-era reads and release packaging; that round is complete. This section
is the current gap list, rebuilt from the repository on 2026-10-14.

**Status: paused 2026-10-19 at Section 508 §602.3 — Electronic Support Documentation.** Intentional, not a completed
government-readiness milestone. Why: substantial installer changes and further visual/UX changes to the customer and
admin apps are planned (section 2, priorities 1–2). Finishing customer installation documentation, verifying its
accessibility and finalizing the VPAT before those changes would mean rework and documenting an outdated product.

- **B2 accessibility baseline (2026-10-19, PRs #229–#233).** Frontend remediation verified in production; automated
  axe-core and blocking JSX accessibility checks; manual keyboard, NVDA, 200% zoom, 320px reflow, text spacing,
  contrast, navigation and form-behavior verification. Evidence: `docs/accessibility-verification-2026-10-19.md` and
  `docs/accessibility-conformance-report.md` (WCAG 2.1 A/AA, criterion by criterion, several rows Partially
  Supports). This is a **dated baseline, not blanket WCAG 2.1 AA or Revised Section 508 conformance.**
- **Local drafts, untracked (not committed as of 2026-10-19), in `docs/accessibility/`:**
  - `SortView_VPAT_508_Draft.docx` — Revised Section 508 edition of VPAT® 2.5Rev, marked "DRAFT … not yet approved
    for procurement publication". Scope: the two production web apps only. Contains WCAG 2.0 A/AA tables (web only;
    Electronic Docs not assessed), Chapter 3 rows all "Pending criterion-specific assessment", Chapter 4 hardware Not
    Applicable, Chapter 5 (502/503 excluded as web-only; §504 Authoring Tools Not Applicable for the evaluated web
    functionality — CSV export and browser print only — reassess if scope changes), Chapter 6 rows pending, and an
    outstanding-verification register.
  - Key VPAT findings: **3.1.1** Language of Page verified Supports (`lang="en"` in source and production, English
    apps); **2.2.1** Timing Adjustable — fixed 14-day session expiry, no warning/extension, unsaved form edits lost on
    expiry; final determination outstanding; **3.3.4** Error Prevention — safeguards reviewed in routing, collector
    settings, account profile, reporting assumptions, membership, installations and lifecycle; final determination
    outstanding.
  - `SortView_Accessibility_and_Support_Guide.md` — "Draft — pending review before public distribution".
- **Support arrangement so far:** primary technical/accessibility support email `sortviewsuper@gmail.com`; electronic
  documentation supplied by email on request; requests for accessible documentation or communication accommodations
  considered individually; **no support-response SLA**. Public `/accessibility` and `/support` pages are possible
  future improvements, **not existing published pages**.
- **None of this is approved, finalized, publicly released or suitable for procurement use.** Keep the drafts internal.

**To resume B (priority 4, after installer and UI changes stabilize):**

1. Update customer installation and support documentation to match the released product. The 1.0.14 guide now
   exists as a Markdown source (`docs/collector-1.0.14-customer-installation-guide.md`, C4); the customer copy in its
   delivered format, and support documentation, are still to do.
2. Complete §602.3 verification of customer-facing electronic documentation, including the DOCX/PDF deliverables.
3. Reassess the Chapter 6 support documentation and services findings (602.2–602.4, 603).
4. Resolve the outstanding VPAT WCAG rows and Chapter 3 classifications from actual evidence.
5. Confirm product version, report scope and applicable standards.
6. Verify the accessibility of the final report itself.
7. Prepare reviewed customer/procurement-facing documentation.

**Done (verified in the repository):**

- Contract v2 is live at NBPL.
  - Intended behavior (by design, not a verified production claim): Contract v2 filters raw patron data on the AMH
    machine before upload.
  - **(verify)** The exact PII behavior of the collector currently deployed in production.
- Row level security on operational event tables; tenant isolation tested.
- Rate limits on uploads, enrollment, login, password change and reset; CORS and security headers on the API.
- HttpOnly, SameSite=Lax session cookies for the API; self-service password reset; lockouts; auth audit log.
- Guest/demo login removed entirely (A5); the historical guest account is deactivated.
- Sentry error tracking with privacy scrubbing (`services/privacy_hardening.py`).
- CI: pytest, ruff, mypy, bandit, pip-audit, Dependabot, frontend test/lint/build; uptime and pipeline-health checks.
- Backup/restore runbook; data lifecycle and offboarding (doc, `tenant_lifecycle_events`, Super Admin offboarding).
- Collector install/update/repair scripts apply least-privilege ACLs (`collector/deploy/CollectorAcl.ps1`).
- Proprietary LICENSE.
- City data-access rule enforced in design: city machines reach data only through the API.

**Gaps:**

| # | Gap | Notes | Depends on |
|---|---|---|---|
| B1 | Customer UI hosted on Streamlit Community Cloud | **Closed (A4, A5):** the customer UI is the React app on `app.sortviewapp.com`, and the Streamlit app is retired. | A |
| B2 | Accessibility conformance evidence | **Baseline done; VPAT paused at §602.3 (2026-10-19)** — see status above. Draft VPAT and support guide are local and untracked. Original note: the production React customer and admin apps now have automated axe coverage, blocking JSX accessibility linting, skip-link and route-arrival focus handling, required-field guidance, document-title fixes, and semantic admin breadcrumb navigation. Manual production verification covered keyboard access, visible focus, 200% zoom, 320px reflow, text spacing, Windows contrast mode, NVDA screen-reader behavior, headings, status/error announcements, meaningful sequence, consistent identification, sensory characteristics and navigation. Evidence is recorded in `docs/accessibility-verification-2026-10-19.md`. Do not claim blanket WCAG 2.1 AA conformance yet. | A |
| B3 | No MFA; no SSO | At minimum MFA for owners/admins; SSO (SAML/OIDC) is often asked for by city IT. | A (UI) |
| B4 | Historical v1 patron data still stored | `acs_events.patron_id`/`raw_message` kept and re-read by v1 hold classification; needs a backfill of classification columns before nulling. v1 `/upload` still open to v1-contract branches. Contract 1 remains technically supported today; its retirement is a deliberate future decision under C5. | — |
| B5 | No customer-facing privacy/data-handling policy | Retention, encryption-at-rest posture (Neon/DigitalOcean), patron-confidentiality statutes, subprocessor list. | — (start early) |
| B6 | No DPA, ToS, SLA templates | Legal review. | B5 |
| B7 | No incident-response plan | Only backup-restore mentions incidents. | — |
| B8 | No security questionnaire pack | Standard answers (HECVAT-style) built from B1–B7. | B1–B7 |
| B9 | No independent security test | Pen test or structured self-assessment of API, React app, Collector. | A, C |
| B10 | Collector binaries are unsigned | Also needed by C. | C |
| B11 | Restore drill evidence | The runbook exists; record a dated, successful restore test **(verify)**. | — |
| B12 | NBPL onsite ACLs | The repair script exists; confirm it has been run on the live machine **(verify)**. | — |

**Exit:** every row above is closed or explicitly accepted, with the evidence linked here.

### C. Installer commercialization

**Goal:** a new library installs and enrolls the Collector from a signed download, following a current guide,
without us on site, and lands in the React app with a working account.

**2026-10-19:** installer improvements are now priority 1 (section 2). Their scope is not yet defined; discovery and
planning come first and decide which C milestones they cover. Nothing below is re-scoped by that decision yet.

**Where it stands (verified):**

- Collector 1.0.14. Server-enforced enrollment (stage A1), fresh-install bootstrap and preflight (A2), fresh
  Contract v2 installation (A3a), Contract v2 as the provisioning default (A3b).
- Install, update, uninstall, repair and verify scripts in `collector/deploy/`; guided `setup-collector.ps1`.
- First ingest moves a branch from `pending` to `ready`, on both contracts (PRs #183, #190).
- **Release record:** `docs/releases/collector-1.0.14.md`. The canonical Collector 1.0.14 bundle was built
  2026-10-24 and is **not yet published**; that record's "Before this release is published" checks are still open.
- **G3, real-API GUI installation, passed 2026-10-25** on a fresh Windows 10 LTSC 2019 VM: all seven stages, SYSTEM
  task run with result 0, synthetic data reached the real API. It used the setup program package built from
  `de858df` (ZIP SHA-256 `e932200f…`), which is **not** the customer package.
- **Approved customer package: G2** -- `SortView-Setup-1.0.14.zip` built from `bef28bc` (PR #268, recovery guidance
  and Copy details), ZIP SHA-256 `6f91da7da2253a4cdef85e4fa6f25789dd8a48a932bc4ae884eac1465ece3193`, as recorded in
  `docs/customer-onboarding-checklist.md` section 6, and now in the release record (*G2 setup program package*). No
  installation of this exact package is recorded. It is an unsigned supervised-pilot package; how Windows warns on
  downloading and running it has not been validated.
- **G2 ZIP and canonical 1.0.14 bundle: recovered and stored (2026-10-28).** Both originals were found on
  `DESKTOP-DORG9IO` under `C:\SortView\ReleaseBuilds\`, matched their recorded SHA-256, passed the secrets and
  library-data inspection, and are stored in `sortview-artifacts` at commit
  `0bf29645868024556df4225c0a0367d42332558b`: `collector/1.0.14/SortViewCollector-1.0.14.zip` and
  `installer/1.0.14/20261025-bef28bc/SortView-Setup-1.0.14.zip`, each with its `ARTIFACT-RECORD.md`. No rebuild or
  successor package is needed. Storage is internal: the bundle is still unpublished and G2 is still an unsigned
  supervised-pilot package. **Pending:** retrieving the exact G2 package on another machine, verifying it against the
  release record and installing it.
- **Centralized artifact storage: in use (2026-10-28).** Canonical Collector ZIPs and setup
  program packages are kept in the private Git repository `richardrhanly-us/sortview-artifacts` (plain Git, no GitHub
  Releases; local clone `C:\Users\rhanly\Projects\sortview-artifacts`), as `collector\<version>\` and
  `installer\<version>\<build-id>\`, each with an `ARTIFACT-RECORD.md`. Each artifact is inspected for secrets and
  library data, copied, hash-verified, then committed and pushed by the product owner. Its release record names the path
  and artifact commit (`docs/release-process.md`, *Preserving release artifacts*). Status: it holds the canonical
  1.0.14 bundle and the G2 package (above). Bundles 1.0.6 to 1.0.13 contain one library's classification rules and
  are not stored there. Not yet demonstrated: storing an artifact on one machine and verifying it from the other.

**Target machine baseline — NBPL's AMH PC, `NEWBRAUNFELSSOR`** (the real Tech Logic sorter computer at New Braunfels
Public Library, where the SortView Collector runs). Build and test installer work against this, not a developer PC.

**Compatibility constraint (product owner, 2026-10-22):** SortView's production installer must support the existing
NEWBRAUNFELSSOR environment, including Windows 10 build 17763 and .NET Framework 4.7.2, without requiring a .NET
Framework or operating system upgrade. This machine's configuration is the deployment compatibility baseline: do not
propose upgrading Windows, .NET Framework, PowerShell or other system components to satisfy installer requirements.

**.NET Framework 4.7.2 compatibility (development gap, not a production-machine defect).** The installer used to target
.NET Framework 4.8 and require release 528040, which NEWBRAUNFELSSOR (4.7.2, release 461814) would fail. Status
(branch `installer/net472-compatibility`, 2026-10-22):

- **Retargeting done:** all seven installer projects (`installer/**/*.csproj`) target **v4.7.2**, and the
  Prerequisites check requires release **461808** or later (`PrerequisiteChecks.MinimumDotNetRelease`), which accepts
  461814.
- **Build validation done:** Release builds of SortView.Setup and SortView.Setup.Spike compile with no warnings
  against Microsoft's 4.7.2 reference assemblies, and the built assemblies declare `.NETFramework,Version=v4.7.2`.
  Building needs the .NET Framework 4.7.2 targeting pack. It is not installed on the development PC, so the
  validation used the official `Microsoft.NETFramework.ReferenceAssemblies.net472` 1.0.3 package (hash checked against
  NuGet).
- **UI Automation window-test failure resolved (test harness only):** a program targeting 4.7.2 keeps collapsed
  WPF elements in the UI Automation tree, marked offscreen. A program targeting 4.8 leaves them out. The window-test
  driver had treated every element in the tree as visible. It now uses UI Automation's `IsOffscreen` property.
  - The normal 4.7.2-targeted build passed all 19 window tests.
  - An experiment with the 4.8 accessibility behavior turned on passed all 18 applicable behavior tests.
  - A new guard test checks that hidden elements are offscreen and cannot take keyboard focus.
  - No production application change and no App.config workaround were needed.
- **Runtime validation (M2c-3) completed for the functions tested, 2026-10-22.** This is runtime compatibility
  validation of the installer as built at `31632e9`, not production-release readiness. NEWBRAUNFELSSOR was not used
  or changed.

  *Environment:*
  - VMware Workstation on the home PC.
  - Windows 10 Enterprise LTSC 2019, build 17763, x64, with .NET Framework 4.7.2 (release 461814).
  - Network disconnected; clean snapshot taken first.
  - Three disposable test packages, each built against Microsoft's genuine 4.7.2 reference assemblies:
    - the default build, with an empty pin
    - a build pinned to a 4-file harmless fixture
    - a build pinned to a 51-file (about 210 MB) harmless fixture
  - The pins were set only in disposable source copies. The repository's `installer/bundle-pin.props` stayed empty.

  *Passed, as seen in the installer window:*
  - It launched on the genuine 4.7.2 runtime.
  - The prerequisites (Windows version, 64-bit, .NET Framework, Windows PowerShell 5.1, administrator rights, disk
    space) passed.
  - The empty-pin build refused preparation with `PinNotConfigured`.
  - The pinned 4-file fixture was verified and staged.
  - The Setup page's installation controls stayed disabled.
  - Cancelling during copying was reported after 5 of 51 files.
  - "Try again" then prepared all 51 files.
  - Clicking the window's close button (X) during active copying closed the window normally.
  - The product owner confirmed that keyboard navigation and Narrator announcements worked.

  *Passed, checked directly on the VM* (folders and permissions, outside the installer):
  - The empty-pin refusal created no `C:\Program Files\SortView`.
  - The staging root is owned by Administrators, with only SYSTEM and Administrators having Full Control.
  - Going back from Prepare to Ready removed the staging run, and re-entering Prepare created a run with a new ID.
  - Exiting after preparation removed the staged run.
  - The cancelled run was removed.
  - Going back after the retry removed the staged run.
  - Closing the window during copying left no staging run.

  *Not tested, so still pending:*
  - **Release packaging:** a signed production release. Only test-only pinned builds and harmless fixtures were
    used.
  - **Collector changes:** any real installation, enrollment or update. Installation stays disabled.
  - **Screen readers in depth:** a full audit of every hidden or collapsed control, and NVDA (only Narrator was
    used).
  - **Failure paths:**
    - forced process termination during staging
    - explicit recovery of an abandoned run (`RemoveAbandonedRun`)
    - cleanup failures caused by locked files

- **Engine execution path (M2d-1 Stage 3B): expected-stop validation passed for the functions tested, 2026-10-22.**
  This was a deliberate stop before enrollment. **No Collector was installed or enrolled.** It shows that an
  installation-enabled Setup page runs the real setup engine and stops safely when SortView cannot be reached. It is
  not a completed installation and not production-release readiness. NEWBRAUNFELSSOR was not used or changed.

  *Context:* by default, installation stays disabled. A build can install only with the build-time installation gate
  on (`installer/installation-gate.props`) and a trusted bundle pin configured (`installer/bundle-pin.props`). The
  repository keeps the gate `false` and the pin empty (M2d-1 Stage 1). Start runs the existing setup engine
  (`setup.ps1`, through `SetupEngineHost`) over the protected staged copy (Stage 2). Immediately before the engine
  runs, the staged copy is verified against the pin again (Stage 3A).

  *Environment:*
  - An isolated VMware VM: Windows 10 Enterprise LTSC 2019, build 17763, with .NET Framework 4.7.2. Network
    disconnected.
  - A disposable installer, built from a source copy with the gate on and the pin set to the fixture. The repository's
    gate and pin were not changed.
  - A 15-file fixture, version `0.0.0-m2d1vm`:
    - the unmodified setup scripts from `collector/deploy`
    - a harmless stand-in `SortViewCollector.exe` that only reports its version (no production Collector executable)
    - an API address of `https://sortview-m2d1-vmtest.invalid`

  *Passed:*
  - The transferred package matched its SHA-256 hashes and file sizes.
  - The installer's prerequisites passed.
  - All 15 fixture files were verified and staged.
  - The enabled Setup page started the real setup engine.
  - The engine completed its initial bundle checks and its computer checks.
  - The engine accepted three harmless placeholder files in place of the Tech Logic logs.
  - The HTTPS request to `https://sortview-m2d1-vmtest.invalid` failed, as intended.
  - The engine stopped at its step 4 (connection) with exit code 1. The wizard shows this as Step 3 of 7.
  - Enrollment, Collector installation, verification and Scheduled Task configuration were not reached.
  - After the installer closed, PowerShell on the VM confirmed:
    - no `C:\SortView\Collector`
    - no `C:\ProgramData\SortViewCollector`
    - no `SortViewCollector` Scheduled Task
    - no staging run folders

  *Not tested, so still pending:*
  - Any real Collector installation, enrollment, update or rollback.
  - Any engine step after the failed HTTPS connection.
  - A production Collector executable.
  - A signed production release.

  *UI issue found (fix separately):* the Welcome and Ready pages show fixed warnings meant for development builds.
  In an installation-enabled build those warnings are wrong.

**Verified on the machine, 2026-10-22** (read-only PowerShell run by the product owner on NEWBRAUNFELSSOR). These are
existing conditions: nothing here is an instruction to change the production machine.

*Operating system and storage*

| Property | Verified value |
|---|---|
| Computer | `NEWBRAUNFELSSOR` (older records call the same computer `NewBraunfelsSorterPC`) |
| Windows | Windows 10 **Enterprise LTSC**, version 10.0.17763, build **17763**, **64-bit** |
| Last boot | 2026-10-19 08:16:12 |
| .NET Framework | **4.7.2**, registry `Release` = **461814** |
| Windows PowerShell | **5.1.17763.9020** |
| Time zone | Central Standard Time (US & Canada), so `America/Chicago` for Contract v2 |
| C: | NTFS; 127,462,797,312 bytes total (~118.7 GiB), 52,745,728,000 bytes free (~49.1 GiB) |

*Accounts and policy*

- Account `NEWBRAUNFELSSOR\Operator`, a member of local Administrators; the elevated PowerShell session ran at High
  Mandatory Level.
- Local Administrators: `Administrator`, `LogMeInRemoteUser`, `Operator`.
- Execution policy `Undefined` in all five scopes; no MachinePolicy or UserPolicy restriction observed.
- WinHTTP: direct access, no proxy.
- The effective AppLocker query returned no rule collections. That is not proof that no application restrictions
  exist.

*Tech Logic logs: `C:\TLCFinalDlls`*

- Files: `ACS Log.txt`, `Checkins.txt`, `Rejects.txt` (the Collector's three sources), plus `Errors.txt`,
  `RoutineMaintenance.txt`, `Statistics.txt`. On 2026-10-22, `ACS Log.txt` and `Checkins.txt` had recent changes;
  `Rejects.txt` was last modified 2026-10-21. Do not assume these names or this layout at other libraries.
- Owner `NEWBRAUNFELSSOR\Operator`. Directory ACL: Users Full Control; Administrators Full Control; SYSTEM Full
  Control; Users ReadAndExecute, Synchronize; Authenticated Users Modify, Synchronize, plus one further entry.
- The Collector runs as SYSTEM, which has Full Control on the directory.

*Existing Collector installation*

- `C:\SortView\Collector` holds `SortViewCollector.exe`, `_internal\`, `logs\`, `MANIFEST.json` and
  `MANIFEST.installed.json`. Both manifests are present; their purpose, structure and origin are not established.
- `C:\SortViewAgent` does not exist.
- **Active Collector: 1.0.13** (verified 2026-10-28; **not** 1.0.14). The running `SortViewCollector.exe` has SHA-256
  `BFCBBFE7D982CE2654C2CC9A069DCF1ABFDD00076A41C5A782149A74BAC66882`, the 1.0.13 release's executable
  (`docs/releases/collector-1.0.13.md`).
- **Recovery ZIP:** `C:\SortViewCollector-1.0.13.zip` is the canonical 1.0.13 release ZIP; it and the running program
  were checked against the release record on 2026-10-28 (that record, *Production status*).
- **Cleanup, 2026-10-28:** nine historical backup folders, four staging folders (the `C:\SortView-1.0.1x-Staging`
  folders), three pilot folders, two old release-build folders and the 1.0.11 release ZIP were **moved** to
  `C:\SortViewCleanup-20261028`. The empty `C:\SortViewPilot` and `C:\SortView\ReleaseBuilds` were removed. The
  2026-10-02 backup (`C:\SortView\Collector.backup-20261002-081805`) stays in place. Nothing historical was deleted, and
  the production configuration, state and Tech Logic files were not touched.
- Nothing shows that the new WPF installer has ever been deployed to this computer; do not assume it has. Neither the
  canonical 1.0.14 bundle nor the G2 package was found on it.

Scheduled Task:

| Property | Verified value |
|---|---|
| Name and path | `SortView Collector`, `\` |
| State when inspected | Ready |
| Runs as | SYSTEM |
| Action | `C:\SortView\Collector\SortViewCollector.exe run` with the configuration `C:\ProgramData\SortViewCollector\config\collector_config.json`, working directory `C:\SortView\Collector` |
| Trigger | Time trigger from 2026-09-18 08:42:40, repeating every 15 minutes (`PT15M`), no repetition duration |
| Settings | MultipleInstances IgnoreNew; execution time limit 1 hour; restart 3 times at 5-minute intervals; StartWhenAvailable True; DisallowStartIfOnBatteries False; StopIfGoingOnBatteries False |
| Last run | 2026-10-28 09:27:27, result `0` (2026-10-22: 09:27:27, result `0`, missed runs 0) |

A last result of 0 shows the task ran and the program exited cleanly. It does not show that uploads to the API
succeeded; the 2026-10-28 checks did not establish data delivery.

*SortView folder ownership and permissions*

| Folder | Verified |
|---|---|
| `C:\SortView` | Owner `NEWBRAUNFELSSOR\Operator`. Administrators Full Control; SYSTEM Full Control; Users ReadAndExecute; Authenticated Users Modify, plus one further entry |
| `C:\ProgramData\SortViewCollector` | Owner `BUILTIN\Administrators`. SYSTEM Full Control; Administrators Full Control |
| `C:\Program Files\SortView` | Does not exist |

**Security consideration for installer planning (not a change request for this machine):** Authenticated Users
can modify `C:\SortView`, the parent of the folder holding the program a SYSTEM task runs. Future installer and upgrade
design must account for this existing condition. The ACL of `C:\SortView\Collector` itself was not captured (see B12).

*Network*

- `sortview-app-2p336.ondigitalocean.app`: DNS resolves (IPv4 and IPv6); TCP 443 connects; source 10.12.100.155 on
  interface "Library Connection"; WinHTTP direct, no proxy. This shows DNS and TCP only, not authenticated API
  operation.

*Microsoft Defender* (no changes made)

- AMRunningMode Normal; real-time protection on; signatures updated 2026-10-21 18:33:44.
- Controlled Folder Access 0 (disabled); no process exclusions.
- Path exclusions: `C:\Program Files (x86)\stunnel`, `C:\Program Files\TLCSoftwareLibrary\`, `C:\TLCFinalDlls\`,
  `C:\VoiceMessages\`. Neither `C:\SortView` nor `C:\ProgramData\SortViewCollector` is excluded.

**Still unverified** (no further machine profiling is planned for now; nothing here may be assumed):

- The Collector configuration file's structure (its path, `C:\ProgramData\SortViewCollector\config\collector_config.json`,
  is the one the task passes; its contents were not inspected).
- Whether the existing Collector uses any direct database connection.
- Permissions on the individual Tech Logic log files, and on `C:\SortView\Collector`.
- Python runtime dependencies, if any (the 2026-08-31 per-user Python 3.14.3 was not re-checked).
- Successful authenticated API communication.
- The full code-integrity policy state.
- Hardware (CPU, RAM; 2026-08-31 values below), and other deployment-specific prerequisites this baseline did not
  establish.
- Whether the onsite ACL repair has been run (B12), and the PII behavior of the deployed Collector (B, verify).

**Superseded capture, 2026-08-31** (screenshots and PowerShell output). Kept as history only. Its hardware and Python
rows were not re-checked, and the legacy agent described next is **contradicted**: `C:\SortViewAgent` no longer exists.

| Area | 2026-08-31 capture |
|---|---|
| Hardware | Generic OEM board (manufacturer and model report "To be filled by O.E.M."); Intel Core **i5-4300U** @ 1.90 GHz, **2 cores / 4 logical processors**; **8 GB RAM** (8,492,486,656 bytes) |
| Python | **3.14.3, 64-bit**, a per-user install under the `Operator` profile; the only version the `py` launcher lists |

**What ran there on 2026-08-31: the legacy SortView agent (no longer present as of 2026-10-22).**

- **Location:** `C:\SortViewAgent`, a Python 3.14.3 `.venv` (last created 2026-04-04) holding:
  - `run_pipeline.py`, `uploader.py`, `config.py`, `logger_config.py`
  - `parse_checkins.py`, `parse_rejects.py`, `parse_acs.py`
  - `agent_config.json`, which holds the API URL
  - `data\` and `logs\` folders
- **Installed packages:** pandas 3.0.2, numpy 2.4.4, requests 2.33.1, SQLAlchemy 2.0.49, psycopg2-binary 2.9.11, certifi 2026.2.25, urllib3 2.6.3, tzdata 2026.1. Database drivers are installed; whether the agent uses them is **(verify)**.
- **Scheduled Task `sortview-scheduler`:** runs `C:\SortViewAgent\.venv\Scripts\python.exe run_pipeline.py`.
  - **Schedule:** daily at 7:00 AM, repeating every 15 minutes indefinitely.
  - **Account:** runs as `Operator` and **only when that user is logged on**, not with highest privileges (the Collector's task runs as SYSTEM instead).
  - **Settings:** restarts on failure every 5 minutes, up to 3 times; stopped after 1 hour; never starts a second instance.
  - **History:** last run on 2026-08-31 succeeded (`0x0`).
- **Tech Logic folder:** a `TLCFinalDlls` folder is pinned in Explorer, matching the Collector's standard source location (`C:\TLCFinalDlls\`).

**Consequences for installer work (future requirements):** the installer must accommodate, with no upgrade of any of
them: Windows 10 Enterprise LTSC build 17763, 64-bit; .NET Framework 4.7.2; Windows PowerShell 5.1; the existing
Collector deployment at `C:\SortView\Collector` and its SYSTEM `SortView Collector` task; the local Tech Logic log
directory; and the existing Windows security and permissions environment (Defender real-time protection with no
SortView exclusions, the broad write access on `C:\SortView`). At NBPL, installing is an upgrade of that existing
deployment, not a fresh install. What remains unverified above must not be assumed from the installer's design.

**Milestones:**

- [ ] **C1. Code signing** for the Collector executable and scripts (needs a certificate — section 6).
- [ ] **C2. One signed download** a library can run, replacing hand-copied bundles. Interim: the unsigned G2
      supervised-pilot package (above), sent with its fingerprint by separate channels. It does not satisfy C2.
- [ ] **C3. First-owner account at provisioning** — **implementation merged** (G6A, PR #269, `7f7cf3e`): a platform
      administrator assigns a library's first owner in React admin, only while it has no active owner, in one locked
      transaction with its audit record; the owner sets a password through "Forgot password?". Its PostgreSQL
      concurrency test (two simultaneous assignments leave exactly one owner) passed against a local disposable
      database on 2026-10-25. **Not yet verified:** a real owner activation in production -- the reset email
      delivered by production SES to an outside address, and that owner signing in.
- [ ] **C4. Current customer install guide** — **written** 2026-10-26 (G6B, PR #270, `5ce3cfc`) as Markdown sources;
      **not delivered**: not yet distributed to a library or accessibility-reviewed. It gives the customer support
      address `sortviewsuper@gmail.com`; that inbox has not been operationally verified (**verify**). Sources:
      `docs/collector-1.0.14-customer-installation-guide.md` (customer, SortView Setup GUI) and
      `docs/customer-onboarding-checklist.md` (internal). They supersede the 1.0.2 guide
      (`docs/SortView_Collector_1.0.2_Customer_Installation_Guide.docx`/`.pdf`) and
      `docs/SortView_Internal_Customer_Provisioning_Checklist.docx`, which are kept unchanged as historical records.
      Still open: a customer copy in the delivered format and its §602.3 review (B, "To resume B" item 1-2), and a
      first production onboarding with them.
- [ ] **C5. Decide the legacy contract's future** — React admin still offers Contract 1 for new installs (as Super
      Admin did).
- [ ] **C6. Deferred Collector work** from `docs/collector-v2.md`: replacement-machine workflow, key rotation,
      installation-status linkage.
- [ ] **C7. Self-serve commercial flow** (Stripe against the existing plans/subscriptions schema; guided signup) —
      last, and only once a second customer is real. Includes deciding customer-visible plan/subscription display
      (deferred here by D8).
- [ ] **C8. Multi-branch / multi-site provisioning** (assigned by A1b decision 5, 2026-10-14) — adding non-primary
      branches and sorter sites; no code path exists today. Not built in A2.

**Exit:** a new library goes from signed download to "ready" with no one on site.

---

## 5. Done recently (newest first)

- 2026-10-28 — Canonical Collector 1.0.14 bundle (`75e7830a…`) and G2 setup package (`6f91da7d…`) recovered and stored
  in `sortview-artifacts` at commit `0bf29645868024556df4225c0a0367d42332558b`; both recorded in
  `docs/releases/collector-1.0.14.md`. Not published or customer-approved by storage; retrieving and installing the
  exact G2 package on another machine is pending (section 4, C).

- 2026-10-28 — Artifact storage workflow documented (`docs/release-process.md`, *Preserving release artifacts*): the
  private `sortview-artifacts` repository, its layout and record fields, the secrets and library-data inspection,
  copy-and-verify, the owner's commit and push, and retrieval and verification on another machine. No artifact stored
  yet (section 4, C).
- 2026-10-28 — NBPL AMH production baseline verified, and the machine cleaned up: active Collector 1.0.13 (executable
  and recovery ZIP match the 1.0.13 release record), task result `0` at 09:27:27 (not proof of API delivery);
  historical folders moved to `C:\SortViewCleanup-20261028`, nothing deleted, production configuration, state and
  Tech Logic files untouched (section 4, C, target machine baseline).

- 2026-10-28 — Live Today "Holds today" and "Bin volume today" (PR #271, `a8a1485`): the customer app built from
  that commit was uploaded to the production S3 bucket and CloudFront invalidated; both sections were seen on the
  production Live Today page. That verifies they are shown, not that their figures are correct (Priority 2 UI work).
- 2026-10-26 — G6B: customer installation guide for Collector 1.0.14 (SortView Setup GUI, enrollment code, recovery
  guidance and Copy details, first-owner sign-in, first data) and the internal onboarding checklist (provision →
  Collector settings → first owner → Contract 2 installation → package and fingerprint → code issued in the session →
  verify Active / Ready / OK), with tenant, owner-email and wrong-library safety checks (C4). The 1.0.2 guide and
  provisioning checklist are kept as superseded historical files. Not verified: production reset-email delivery, a
  production onboarding, code signing.
- 2026-10-25 — G3 real-API GUI installation passed with the `de858df` package (recorded in
  `docs/releases/collector-1.0.14.md`, PR #267). G2 recovery guidance and Copy details merged (PR #268, `bef28bc`);
  the G2 package built from it is the approved customer package (section 4, C). G6A first-owner assignment merged
  (PR #269, `7f7cf3e`), with its PostgreSQL concurrency test passing locally (C3).
- 2026-10-22 — M2d-1 Stage 3B expected-stop validation passed on an isolated Windows 10 Enterprise LTSC 2019 VM
  (build 17763, .NET Framework 4.7.2, network disconnected). The test used a disposable installation-enabled build,
  a 15-file harmless fixture (`0.0.0-m2d1vm`) and a stand-in Collector executable.
  - The Setup page started the real setup engine.
  - The engine passed its bundle and computer checks.
  - It stopped as intended at the failed HTTPS connection (its step 4; Step 3 of 7 in the wizard), with exit code 1.
  - Nothing was installed or enrolled: no install or data folder, no Scheduled Task, and no staging run were left.

  This was a deliberate stop before enrollment, not a Collector installation. Still pending: real installation,
  enrollment, update and rollback; engine steps after the connection check; a production Collector executable; and a
  signed production release. UI issue to fix separately: the Welcome and Ready pages show development-build warnings
  that are wrong in an installation-enabled build (section 4, C).
- 2026-10-22 — M2c-3 runtime validation of the installer passed on Windows 10 Enterprise LTSC 2019 (build 17763)
  with .NET Framework 4.7.2, in an isolated VM, using test-only pinned builds and harmless fixtures. Covered:
  - verified protected staging
  - cancellation during copying, and retry
  - cleanup of every staging run
  - installation controls staying disabled
  - basic keyboard and Narrator accessibility

  Still pending: a signed production release; real installation, enrollment and update; a full screen-reader audit
  (including NVDA); and the forced-termination, abandoned-run-recovery and locked-file failure paths (section 4, C).
- 2026-10-22 — NBPL AMH machine (`NEWBRAUNFELSSOR`) baseline verified from the product owner's read-only checks:
  Windows 10 Enterprise LTSC build 17763, 64-bit, .NET Framework 4.7.2 (461814), PowerShell 5.1.17763.9020; Collector
  at `C:\SortView\Collector` run by the SYSTEM task `SortView Collector` every 15 minutes; Tech Logic logs in
  `C:\TLCFinalDlls`; `C:\SortViewAgent` is gone. Compatibility constraint recorded: no OS or .NET upgrade may be
  required (section 4, C).

- 2026-10-19 — B2 accessibility baseline: frontend remediation, automated axe/JSX checks and manual verification
  (PRs #229–#231); evidence and WCAG 2.1 conformance report (PRs #232, #233). A dated baseline, not a conformance
  claim. Government readiness then paused at §602.3; draft VPAT and support guide kept local (section 4, B).
- 2026-10-17 — A5: customer Streamlit retired. Hosted app deleted; source, tests and tooling removed (PRs #215,
  #216); Streamlit, Altair and WeasyPrint dependencies and `packages.txt` removed (PR #217); guest account
  deactivated; stale `security` JSON removed from four `organization_settings` rows
  (`docs/production-verification-runbook.md` Part 5). Workstream A complete.
- 2026-10-16 — A4: NBPL customer React cutover completed. `https://app.sortviewapp.com` is the production customer
  entry point, on the reused customer CloudFront distribution and S3 bucket; the smoke test and the client-IP check
  passed (`docs/production-verification-runbook.md` Part 4). Customer Streamlit remains as rollback until A5.
- 2026-10-15 — A2 item 8: Super Admin Streamlit retired; the hosted app deleted and its code removed (PR #212).
- 2026-10-15 — A3: React admin in production at `https://admin.sortviewapp.com`, on its own CloudFront distribution
  and private S3 bucket, ACM certificate and Cloudflare DNS. DigitalOcean origins set, and items 1–7 smoke-tested in
  production with no customer or Collector regression (`docs/production-verification-runbook.md` Part 3).
- 2026-10-15 — A2 items 1–7 all merged; the last, item 7, is PR #210 (`64c7d7b`).
- 2026-10-14 — A1 (parity checklist, D1–D9) and A1b (Super Admin parity audit, decisions 1–5) completed.
- 2026-10-14 — React "Waiting for first data" state (PR #191, merged).
- 2026-10-14 — Legacy v1 uploads mark a pending branch ready (PR #190).
- 2026-10-14 — Customer API exposes normalized sorter `onboarding_status` (PR #189).
- 2026-10-13/14 — Pipeline rules, access mode, settings merge and branch readiness extracted from Streamlit
  (PRs #184, #186–#188); legacy unknown pipeline health handled (PR #185).
- 2026-10-13 — Contract v2 first ingest makes a branch ready (PR #183).

---

## 6. Long-lead items safe to start in parallel

Not engineering, but slow; starting them early shortens the plan without moving it.

- Code-signing certificate procurement (C1, B10).
- SortView domain and DNS (A3): **done 2026-10-15**. The domain is `sortviewapp.com` on Cloudflare DNS; the ACM
  certificate for `app.` and `admin.` is issued.
- Legal drafting/review: privacy policy, DPA, ToS, SLA (B5, B6).
- Ask a target city IT contact for their actual vendor questionnaire (shapes B8).

---

## 7. Parked

One line each, dated. Pick up only when a milestone needs it, or when it is promoted on purpose.

- 2026-10-14 — Fact-only Live Today reject-rate notice (vs. that sorter's own baseline, after a minimum check-in volume, no "check X" advice) — only if NBPL or a customer asks or shows it is useful (D4). Not an A2 requirement.
- 2026-10-14 — Branch Services / Collection Services counts in the ranged Holds report — reconsider only if NBPL staff confirm they drive a real operational decision (D3). Not an A2 requirement.
- 2026-10-14 — Optional single printable React summary view (accepted metrics only) — build only if a customer or director asks for it (D1).
- 2026-10-14 — NBPL's only subscription has status `trial`; `get_org_subscription` picks the newest subscription regardless of status. No effect today; decide before billing (C7).
- 2026-10-14 — `onboarding_status` vocabulary: `paused`/`mapping`/`settings`/`agent`/`initial_sync` have messages but no writer; no admin control; no DB CHECK constraint (production holds only `pending`/`ready`).
- 2026-10-14 — `branches.status` has no admin control (inactive only via SQL); Super Admin warns but cannot change it.
- 2026-10-14 — React pending page does not refresh to "ready" while open (organization detail is fetched once); a "Check again" button would fix it.
- 2026-10-14 — `docs/collector-v2.md` (around line 440) still describes only the v2 first-ingest transition.
- 2026-10-14 — `tests/test_collector_setup.py` token-wrapper tests fail on a byte-order mark in the stdin capture file (pre-existing).
- 2026-10-14 — `app.py` checks operational mapping twice (readiness gate and line ~652). **Resolved 2026-10-17:** `app.py` was removed (A5).
- 2026-10-14 — Two stale unmerged branches from 2026-09-25, `fix/live-today-controls-layout` and `fix/live-today-spacing`: decide merge, rebase or delete. Not a blocker for A1.
- 2026-10-15 — `ingest_v2_service.latest_ingest_status` has no callers (dead); kept unchanged in A2 item 3 by decision. Delete or reuse later.
- 2026-10-15 — After A2 item 3, `render_live_today` still receives the run/collector diagnostics args (attempt, status code, counters, destination breakdown, collector lines) and `_format_count` is unused; trim when Streamlit Live Today is retired. **Resolved 2026-10-17:** removed with the Streamlit code (A5).
- 2026-10-15 — `test_when_keeps_its_whole_second_format_and_its_delta_semantics` fails when the full suite runs more than ~1h after import (wall-clock dependence).
- 2026-10-15 — `src/branch_settings.json` is tracked in a public repository with one library's real Internal Routing lists (institutional account labels; no patron-card numbers found) and its names; git history holds the same plus an old admin password. Replace with synthetic data of the same shape; decide on a history rewrite separately. Not part of A2 item 4.
- 2026-10-15 — `lines_to_list` in `src/pages/1_admin_settings.py` is unused since A2 item 4 removed the Internal Routing form; remove with the Streamlit settings page. **Resolved 2026-10-17:** removed with the page (A5).
- 2026-10-15 — `collector_installations.required_contract` is fixed only by convention (no service updates it); there is no database-level immutability.
- 2026-10-15 — A Contract 1 installation enrolled again leaves its earlier bound agent tokens active.
- 2026-10-15 — An unused enrollment code can be revoked only by issuing a new one (or by offboarding); there is no revoke-only action.
- 2026-10-15 — The item-2 sorter list (and the customer's) hides installations on inactive branches; the item-5 admin Installations list shows every installation. Intentional; revisit with C8.
- 2026-10-15 — Reversing an offboarding (`access_cutoff_reverted`) remains manual and operator-only; no tool or admin control exists (A2 item 6 deliberately adds none).
- 2026-10-15 — The React Collector configuration page does not know a library is cancelled: its forms look editable until a save is refused with 409 `library_cancelled` (clear message). Its responses carry no library status.
- 2026-10-15 — Suspension leaves unused enrollment codes stored and unrevoked; redemption is refused while suspended, but a code still within its 30-minute TTL can be redeemed after reactivation.
- 2026-10-15 — Super Admin suspend/reactivate still write no audit row (React admin does, A2 item 6); goes away when Super Admin retires (item 8). Code removed by item 8; resolved once it merges.
- 2026-10-15 — The `internal_workflow` (holds), `transits` and `max_sorters` entitlements are not seeded by any migration; a newly provisioned library may lack features an existing one has (C7).
- 2026-10-15 — The database does not enforce exactly one primary branch per organization, and `branches.organization_id` has no foreign key; provisioning creates exactly one by construction.
- 2026-10-15 — Organization and branch rename have no React path (D8 admin scope).
- 2026-10-15 — Legacy installer values / `agent_config.json` are still referenced as Super Admin output by Collector-shipped text (`collector/preflight.py`, `setup-collector.ps1`, `install-release.ps1` help, `collector_config.example.json`); change with the next Collector release (C5/C2). `docs/collector-v1-admin-guide.md` now says React admin does not show them yet (A2 item 8).
- 2026-10-15 — No first-owner flow or documented procedure exists; a newly provisioned library has no one who can sign in (C3).
- 2026-10-15 — Super Admin's `slugify` can produce an empty slug, and its provisioning stays three non-atomic commits, until Super Admin retires (item 8). Code removed by item 8; resolved once it merges.
- 2026-10-15 — After A2 item 8, these service helpers have no production caller (kept on purpose; some are C5/C2 compatibility material): `tenant_service.format_collector_install_parameters`, `_require_installation_id`, `build_collector_agent_config` and the legacy agent-config path constants, `create_collector_installation` (wrapper), `update_collector_installation`, `list_collector_installations_for_organization`, `set_collector_site_settings`; `collector_enrollment_service.generate_enrollment_code_for_installation`; `platform_admin_service.list_libraries_with_status`. Remove or reuse later.
- 2026-10-15 — Support contacts and internal notes are no longer collected at provisioning (React); a proper admin feature for them is still to be decided (D8 admin scope).
- 2026-10-15 — Node engine mismatch: the admin build ran on Node v24.12.0, while some dependencies ask for >=24.15.0 (or qualifying 22/26 releases). The build succeeded and `npm audit` reported 0 vulnerabilities. Align the local Node version (and `frontend/.nvmrc`, `24`) before the next frontend release.
- 2026-10-15 — The A3 client-IP check is an observation only: the admin requests came from one consistent source address in the DigitalOcean logs, consistent with viewer-IP preservation. The external-IP cross-check was not completed because of a local Windows TLS trust/SNI failure (unrelated to SortView). **Resolved 2026-10-16:** the exact check passed on the customer path during A4 (`docs/production-verification-runbook.md` Part 4).
- 2026-10-15 — Local Windows TLS trust/SNI failure on the home PC blocked an external check-IP request; investigate separately.
- 2026-10-15 — Production `alembic current` cannot be read with the runtime role (`permission denied for table alembic_version`, by design). The schema is proven only functionally, plus deployed head `f7b3d2c81a46`. Read it with the migration/owner role when one is next used; do not widen the runtime role.
- 2026-10-15 — The disposable production library "SortView Admin Smoke Test" (`sortview-admin-smoke-test`) is cancelled but not purged; purging it is an operator decision (`scripts/purge_tenant_data.py`).
- 2026-10-15 — `SORTVIEW_CUSTOMER_ALLOWED_ORIGINS` still lists the customer React trial's CloudFront origin. Kept deliberately through the A4 soak (2026-10-16); remove it after the soak (A5). **Superseded (A5):** removing it broke customer writes from `app.sortviewapp.com` in production, so it must stay (A5). Why production needs it is not yet explained; investigate only as its own task.
- 2026-10-19 — The tracked `docs/accessibility-conformance-report.md` (WCAG 2.1) predates the VPAT draft: it still rates 3.1.1 Partially Supports and has older 2.2.1/3.3.4 wording. Reconcile when B resumes (priority 4).
- 2026-10-01 — `ruleset_id`: `docs/contract-v2-design.md` §5 contradicts the deliberate code behavior; fix the doc or the code.
- 2026-10-27 — The G3 test installation (`SortView G3 Installer Test`, installation 7) is still `active` while its task is disabled, so the scheduled pipeline-health check fails on its stale heartbeat. Fix: set that installation to Inactive in React admin; no code change (**verify** whether done).

---

## Change log

- 2026-10-14 — Manifest created. Order set to: Phase 0 → A (React cutover) → B (government readiness) → C (installer
  commercialization), with long-lead items in parallel.
- 2026-10-14 — Workstream A boundary widened (product owner, prompted by D6): the React migration includes the
  internal provisioning/library-management surface; Super Admin may no longer stay on Streamlit indefinitely. Added
  A1b (Super Admin parity audit after D9) as a prerequisite to A2. Workstream order unchanged.
- 2026-10-17 — A5 completed, and with it Workstream A. Next is B (government readiness), then C (installer
  commercialization); order unchanged.
- 2026-10-19 — Order revised (product owner): B paused at Section 508 §602.3 after the B2 accessibility baseline.
  New order: 1 installer improvements → 2 UI redesign and polish → 3 targeted accessibility regression verification →
  4 resume B. Why: planned installer and UI changes would make the installation documentation and VPAT out of date.
  Scope for 1 and 2 is not yet defined.
