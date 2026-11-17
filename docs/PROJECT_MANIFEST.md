# SortView project manifest

The big picture, kept in one place so a single task does not swallow the plan. Read this before starting any
non-trivial task, and check the task against it before widening scope.

Last reviewed: 2026-11-17 (Collector 1.0.15 canonical bundle built; NBPL production sorter profile, Collector 1.0.15 backup security and the upgrade status; G2 qualification entries
2026-11-12; the rest as of 2026-10-28). Facts marked **(verify)** were not confirmed against production or the person who
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
- **Collector 1.0.15 (planned, 2026-11-16):** it supersedes 1.0.14 for upgrading existing installations (NBPL), with
  restricted, verified upgrade backups and a verified rollback.
  - Merged to `main` (PR #303, `c1b161e`). Canonical bundle **built and verified 2026-11-17** from `2565b95` (ZIP
    SHA-256 `6b358993…9ec8`), stored in `sortview-artifacts` at `09130bd…`; **not rehearsed or released**.
  - Record: `docs/releases/collector-1.0.15.md`.
  - Status and blockers: *NBPL 1.0.13 → 1.0.15 upgrade status* below.
- **G3, real-API GUI installation, passed 2026-10-25** on a fresh Windows 10 LTSC 2019 VM: all seven stages, SYSTEM
  task run with result 0, synthetic data reached the real API. It used the setup program package built from
  `de858df` (ZIP SHA-256 `e932200f…`), which is **not** the customer package.
- **Approved customer package: G2** -- `SortView-Setup-1.0.14.zip` built from `bef28bc` (PR #268, recovery guidance
  and Copy details), ZIP SHA-256 `6f91da7da2253a4cdef85e4fa6f25789dd8a48a932bc4ae884eac1465ece3193`, as recorded in
  `docs/customer-onboarding-checklist.md` section 6, and now in the release record (*G2 setup program package*). It is
  an unsigned supervised-pilot package; how Windows warns on downloading and running it has not been validated.
- **G2 exact-package GUI installation passed 2026-10-29** on a Windows 10 LTSC 2019 VM (installation `8`, since
  deactivated): all seven stages, exit code `0`; manual and automatic SYSTEM task runs exit code `0`. The automatic run
  happened only **after the trigger was edited** (following a VM time-zone correction); why the original trigger
  missed its runs is not established. Tech Logic files were empty placeholders, so ingesting realistic events through
  G2 is not validated. Recorded in `docs/releases/collector-1.0.14.md`, *G2 validation*.
- **G2 fresh installation and end-to-end ingestion passed 2026-11-12** on a clean Windows 10 LTSC 2019 VM snapshot,
  same package (`6f91da7d…`), library `sortview-g2-qualification-2`, installation `9` (Contract v2, left active): all
  seven stages, exit code `0`; heartbeat healthy, onboarding Ready, pipeline OK. The first automatic run after
  installation (10:43:43 AM CST) and the later runs at 10:58 and 11:13 AM all came from the **original, unedited
  trigger**, each with exit code `0`. This does not explain why installation `8`'s trigger missed its runs on
  2026-10-29; that cause is still unknown.
  PostgreSQL holds 13 events for installation `9`: ACS 5 hold, 1 non-hold 101, 1 other code-10; 4 check-ins; 2
  rejects. 0 quarantined, 0 pending, 0 server-reported duplicates. Two of those check-in/reject events come from a
  **test-harness error** (a PowerShell fixture that left out the ACS `0x02` separators), not a Collector or installer
  defect. Resume, upgrade, repair, uninstall and accessibility were not part of this test. Recorded in
  `docs/releases/collector-1.0.14.md`, *G2 validation: fresh installation and end-to-end ingestion*.
- **G2 ZIP and canonical 1.0.14 bundle: recovered and stored (2026-10-28).** Both originals were found on
  `DESKTOP-DORG9IO` under `C:\SortView\ReleaseBuilds\`, matched their recorded SHA-256, passed the secrets and
  library-data inspection, and are stored in `sortview-artifacts` at commit
  `0bf29645868024556df4225c0a0367d42332558b`: `collector/1.0.14/SortViewCollector-1.0.14.zip` and
  `installer/1.0.14/20261025-bef28bc/SortView-Setup-1.0.14.zip`, each with its `ARTIFACT-RECORD.md`. No rebuild or
  successor package is needed. Storage is internal: the bundle is still unpublished and G2 is still an unsigned
  supervised-pilot package. **Pending:** retrieving the exact G2 package from the artifact repository on another
  machine and verifying it against the release record.
- **Centralized artifact storage: in use (2026-10-28).** Canonical Collector ZIPs and setup
  program packages are kept in the private Git repository `richardrhanly-us/sortview-artifacts` (plain Git, no GitHub
  Releases; local clone `C:\Users\rhanly\Projects\sortview-artifacts`), as `collector\<version>\` and
  `installer\<version>\<build-id>\`, each with an `ARTIFACT-RECORD.md`. Each artifact is inspected for secrets and
  library data, copied, hash-verified, then committed and pushed by the product owner. Its release record names the path
  and artifact commit (`docs/release-process.md`, *Preserving release artifacts*). Status: it holds the canonical
  1.0.14 bundle and the G2 package (above). Bundles 1.0.6 to 1.0.13 contain one library's classification rules and
  are not stored there. Not yet demonstrated: storing an artifact on one machine and verifying it from the other.

#### Canonical NBPL production sorter profile — `NEWBRAUNFELSSOR`

**This is the one profile of NBPL's AMH PC** (the real Tech Logic sorter computer at New Braunfels Public Library,
where the SortView Collector runs). Build and test installer work against this, not a developer PC.

**Consult this profile before asking the operator for any machine command.** Do not ask for a check recorded here to be
rerun unless there is a specific reason: the installation changed (an upgrade, repair or rollback), an observation
conflicts with this record, or a new validation requirement needs a value not captured here. Add every new machine
observation here with its date; mark superseded values rather than deleting them.

Verification history: 2026-08-31 (superseded capture, below), 2026-10-02 (1.0.13 production update,
`docs/releases/collector-1.0.13.md`), 2026-10-22 (read-only baseline), 2026-10-28 (executable hash, recovery ZIP,
cleanup), **2026-11-16 (installed manifest, scheduled task, ACLs and data root; latest)**.

**Verified on the machine, 2026-11-16** (product owner's read-only checks; nothing was changed):

| Area | Verified value |
|---|---|
| Installed product | SortView Collector, bundle version **1.0.13** (production is **not** on 1.0.14) |
| Installed manifest | `C:\SortView\Collector\MANIFEST.installed.json`: bundle built `2026-10-02T00:33:47.407285Z`, installed `2026-10-02T13:18:55.030162Z` (08:18:55 CDT, matching the 1.0.13 production update and the backup's name) |
| Executable | `C:\SortView\Collector\SortViewCollector.exe` (hash last checked 2026-10-28: the 1.0.13 executable, `BFCBBFE7…`) |
| Runtime backup | `C:\SortView\Collector.backup-20261002-081805` still present (made by the 1.0.13 update) |
| Scheduled task | `SortView Collector`; runs as SYSTEM; run level **Highest**; arguments `run --config "C:\ProgramData\SortViewCollector\config\collector_config.json"`; working directory `C:\SortView\Collector`; nominal interval 15 minutes |
| Last and next run | Last run 2026-11-16 2:12:12 PM local, `LastTaskResult` `0`; next run 2:27:27 PM local |
| Persistent data root | `C:\ProgramData\SortViewCollector`, with `config`, `data`, `logs`, `secrets`. Persistent state is **not** under `C:\SortView\runtime` |

ACLs, 2026-11-16:

| Path | Verified ACL |
|---|---|
| `C:\SortView` (parent) | **Authenticated Users Modify**; BUILTIN\Users Read & Execute; Administrators and SYSTEM Full Control |
| `C:\SortView\Collector` | Administrators and SYSTEM Full Control only |
| `C:\SortView\Collector\SortViewCollector.exe` | Administrators and SYSTEM Full Control only |
| `C:\ProgramData\SortViewCollector` | Administrators and SYSTEM Full Control only |
| `C:\ProgramData\SortViewCollector\config` | Administrators and SYSTEM Full Control only |
| `C:\ProgramData\SortViewCollector\data` | Administrators and SYSTEM Full Control only |
| `C:\ProgramData\SortViewCollector\logs` | Administrators and SYSTEM Full Control only |
| `C:\ProgramData\SortViewCollector\secrets` | Administrators and SYSTEM Full Control only |
| `C:\SortView\Collector.backup-20261002-081805` | **Inherited from `C:\SortView`:** Administrators and SYSTEM Full Control, BUILTIN\Users Read & Execute, **Authenticated Users Modify**, all inherited by child files and folders |

What these show, and do not show:
- The task last **completed** with exit code 0. That does not prove that every upload reached the API. Delivery is
  checked server-side (heartbeat, pipeline health), not by another machine command.
- `MANIFEST.json` is an exact copy of the bundle's manifest. `MANIFEST.installed.json` is the installed-layout view
  that `Test-InstalledManifest` verifies against (`collector/deploy/CollectorManifest.ps1`). This settles the
  2026-10-22 question about the two manifests.
- The restricted ACL on `C:\SortView\Collector` matches what `tools\update.ps1` applies after an update
  (`Protect-CollectorPath`, `collector/deploy/update-release.ps1`). It is consistent with B12's repair having been
  applied, but not proof that `tools\repair-permissions.ps1` itself was run.

**Security conclusion (2026-11-16):** the production Collector (`C:\SortView\Collector`, its executable) and every
persistent-data directory (`C:\ProgramData\SortViewCollector` and its `config`, `data`, `logs`, `secrets`) are
restricted to Administrators and SYSTEM.

**Confirmed backup-permissions vulnerability (open).**
- The existing backup `C:\SortView\Collector.backup-20261002-081805` inherits `C:\SortView`'s ACL, so any
  authenticated user can modify it.
- **Cause:** `tools\update.ps1` (`collector/deploy/update-release.ps1`) creates every backup as
  `C:\SortView\Collector.backup-<timestamp>` (and, in source mode, `…-full`) by copying into the parent. It never
  restricts the copy, so **every future upgrade backup will have the same exposure** until the script is fixed.
- **Why it matters:** the rollback the script prints copies that backup, unverified, back into the folder the SYSTEM
  task runs from. A user-modified backup restored that way would run as SYSTEM.
- **Tampering:** none was observed or established. The finding is that the backup *could* have been modified, not
  that it was.
- **Fix:** implemented for Collector 1.0.15 (2026-11-16) but **not released, rehearsed or deployed**. NBPL still runs
  1.0.13, and its existing backup is unchanged. See *NBPL 1.0.13 → 1.0.15 upgrade status* below.
- **Consequences:**
  - This backup **must not be treated as a trusted rollback source** without integrity verification.
  - It is also not a 1.0.13 rollback source at all: it holds the runtime from *before* the 2026-10-02 update to
    1.0.13.
  - A 1.0.13 rollback should come from the canonical recovery ZIP `C:\SortViewCollector-1.0.13.zip`
    (hash-verified 2026-10-28), checked against `docs/releases/collector-1.0.13.md` again before use.
- **Still to review:** whether a non-administrator can rename, replace or pre-create entries beside
  `C:\SortView\Collector` in the parent folder. For example, they might pre-create a predictable backup path or a
  junction before an update.

The sections below are the earlier captures, kept as recorded; where 2026-11-16 adds to or supersedes them, they say so.

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
  `MANIFEST.installed.json`. Both manifests are present. *(2026-11-16: their purpose is established from
  `collector/deploy/CollectorManifest.ps1`; see the profile above.)*
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
| Last run | 2026-11-16 2:12:12 PM, result `0` (2026-10-28: 09:27:27, result `0`; 2026-10-22: 09:27:27, result `0`, missed runs 0) |
| Run level | Highest (first recorded 2026-11-16) |

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
design must account for this existing condition. *(2026-11-16: the ACLs of `C:\SortView\Collector`, its executable and
`…\config` were captured, all Administrators and SYSTEM only; the parent-folder concern stays open, see the profile
above.)*

*Network*

- `sortview-app-2p336.ondigitalocean.app`: DNS resolves (IPv4 and IPv6); TCP 443 connects; source 10.12.100.155 on
  interface "Library Connection"; WinHTTP direct, no proxy. This shows DNS and TCP only, not authenticated API
  operation.

*Microsoft Defender* (no changes made)

- AMRunningMode Normal; real-time protection on; signatures updated 2026-10-21 18:33:44.
- Controlled Folder Access 0 (disabled); no process exclusions.
- Path exclusions: `C:\Program Files (x86)\stunnel`, `C:\Program Files\TLCSoftwareLibrary\`, `C:\TLCFinalDlls\`,
  `C:\VoiceMessages\`. Neither `C:\SortView` nor `C:\ProgramData\SortViewCollector` is excluded.

**Still unverified** (updated 2026-11-16; nothing here may be assumed):

- The Collector configuration file's contents (its path, `C:\ProgramData\SortViewCollector\config\collector_config.json`,
  is the one the task passes; its contents were not inspected). NBPL's Contract v2 cutover (2026-09-28) and the
  1.0.13 dashboard's "Contract v2 collector reporting healthy" say it runs v2. Its `contract_mode` was not read on the
  machine.
- Whether the existing Collector uses any direct database connection. *(The 1.0.13 production update on 2026-10-02
  recorded "HTTPS-only / no direct database dependency: PASS", `docs/releases/collector-1.0.13.md`.)*
- Permissions on the individual Tech Logic log files. *(`C:\SortView\Collector`: captured 2026-11-16.)*
- *(Captured 2026-11-16: the ACLs of `…\secrets`, `…\data`, `…\logs` and the 2026-10-02 backup; see the profile.)*
- Whether a non-administrator can rename, replace or pre-create entries in `C:\SortView` (the open review item above).
- Whether the 2026-10-02 backup's contents still match what was copied. Its own `MANIFEST.installed.json`, if it
  has one, sits in the same user-modifiable tree and cannot vouch for it.
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

**NBPL 1.0.13 → 1.0.15 upgrade status (updated 2026-11-16). Production remains on 1.0.13. The upgrade is NOT ready.**

**Planned release 1.0.15 (decided 2026-11-16, product owner): it supersedes 1.0.14 as the release for NBPL's upgrade.**
It carries the backup-permissions fix (blocker 3 below). Release notes: `docs/releases/collector-1.0.15.md`.
- **Merged to `main` (PR #303, merge `c1b161e`, 2026-11-16).**
- **Canonical bundle built and verified 2026-11-17** on `DESKTOP-DORG9IO` from `2565b95` (docs-only after `c1b161e`):
  `SortViewCollector-1.0.15.zip`, SHA-256 `6b3589938b5c255dd74e2a89537a9e1c1db63d84728f605e7e8495789dc59ec8`;
  `MANIFEST.json` SHA-256 `50eac4ca…eddab`; 1394 entries. Hashes, script checks and the secrets inspection (0 findings)
  are in `docs/releases/collector-1.0.15.md`, *Canonical release artifact*.
- **Stored 2026-11-17** in `sortview-artifacts`: `collector/1.0.15/SortViewCollector-1.0.15.zip` with its
  `ARTIFACT-RECORD.md`, commit `09130bde1c45076028482a1fb0d63cbb63873523`. Storage is not approval.
- **Not rehearsed, not released, not production-ready.** NBPL stays on 1.0.13.
- `collector.__version__` is `1.0.15`.
- **What changed:**
  - `tools\update.ps1` (`collector/deploy/update-release.ps1`): the backup folder is created already restricted,
    pre-existing or redirected paths are refused, and the copy is restricted, re-owned and verified before the install
    is removed. The frozen install root is recreated restricted.
  - New `tools\restore-backup.ps1`: a verified rollback, anchored to a `MANIFEST.json` hash obtained outside the backup.
  - New shared library `tools\CollectorBackup.ps1`.
  - `CollectorManifest.ps1` gains `Get-CollectorInstalledLayout`.
- **Automated validation, 2026-11-16, on the development PC** (Windows PowerShell 5.1, **not elevated**):
  - see *1.0.15 validation evidence* below.
- **NBPL's existing 2026-10-02 backup** is not restricted, so `restore-backup.ps1` refuses it by design.
- **Rollback anchor for 1.0.13:** the 1.0.13 release record has no *Bundle `MANIFEST.json` SHA-256*. A rollback to
  1.0.13 uses `-TrustedManifestPath` with the `MANIFEST.json` of the verified `SortViewCollector-1.0.13.zip`.

The 1.0.14 records below are kept as they were. **Do not use the 1.0.14 bundle to upgrade an existing installation:**
its `tools\update.ps1` has the backup vulnerability.

*1.0.14 artifacts, existing and hash-recorded (unchanged):*
- **Canonical bundle:** `SortViewCollector-1.0.14.zip`, SHA-256 `75e7830a…501f5`.
- **G2 setup program package:** `SortView-Setup-1.0.14.zip`, SHA-256 `6f91da7d…3193`.
- Both matched their recorded hashes when stored in `sortview-artifacts` at `0bf2964…` on 2026-10-28, from
  `DESKTOP-DORG9IO`. G2's hash was verified again before the 2026-10-29 test and in R4 Stage 0 (2026-11-13).
- Not done: retrieving either artifact on another machine (the NBPL PC included) and verifying it there. Neither is on
  NBPL (2026-10-28).
- The bundle is **unpublished**, and G2 is an unsigned supervised-pilot package.

*Upgrade path:*
- For an existing install it is `tools\update.ps1` (= `collector/deploy/update-release.ps1`) from the canonical
  bundle: the **1.0.15** bundle (built 2026-11-17, ZIP SHA-256 `6b358993…9ec8`), once the rehearsal passes.
- Release notes, *Compatibility*: it keeps an existing Contract v2 install's config, secret, rules and cursor.
- 1.0.15's `tools\update.ps1` refuses a frozen install without `MANIFEST.json` and `MANIFEST.installed.json`. NBPL
  has both.
- **The G2 setup program is not an upgrade path for NBPL.** 1.0.14's setup refuses a machine that already holds an API
  token, a v2 secret, a cursor or a rules file (release notes, *A first install starts from a clean machine*).

*Tests that have passed, none of them a 1.0.13 → 1.0.14 or 1.0.15 upgrade:*
- G3 (2026-10-25) and G2 (2026-10-29 and 2026-11-12): **fresh** installations on VMs.
- M2c-3 and M2d-1 (2026-10-22): installer staging, and a stop before enrollment.
- The M2d-4 P0C/P1/P1R VM tests used an earlier, pre-PR #266 1.0.14 runtime. Their scope is not recorded in the
  repository, so they are not upgrade evidence for the canonical bundle.
- The one recorded in-place production update is **to 1.0.13 on NBPL, 2026-10-02**, from the canonical 1.0.13 ZIP
  (`docs/releases/collector-1.0.13.md`; the record does not name the prior version). Its backup naming matches
  `tools\update.ps1`. That proves the mechanism for that release, not for 1.0.14.

*Not done:*
- **No 1.0.13 → 1.0.14 or 1.0.15 upgrade test is recorded anywhere.**
- **No rollback test** is recorded. The 2026-10-02 update's backup was never restored.

*1.0.15 validation evidence (2026-11-16, development PC, Windows PowerShell 5.1, not elevated):*
- **New `tests/test_collector_backup_security.py`: 40 passed, 2 skipped.** It covers:
  - order: the backup is created restricted, then copied, then verified, all before the install is removed;
  - the backup path is checked before the task is touched;
  - backup failure aborts before any change;
  - the source `-full` folder and the frozen install root are recreated restricted;
  - redirect checks: junctions, paths under a junction, and junctions inside a backup;
  - pre-existing folder, file, junction and dangling-junction paths are refused and left alone;
  - copy and recorded-file integrity, with NBPL's parser logs allowed;
  - the trust anchor:
    - an altered file is refused;
    - a manifest rewritten to match is refused;
    - wrong or malformed anchors are refused;
    - a missing file is refused;
    - a source-kind backup is refused;
    - the backup's own `MANIFEST.installed.json` is never trusted;
  - an NBPL-shaped backup reads as insecure;
  - the restore order, its failure path (task left disabled) and its verified-only copies;
  - a non-elevated restore refuses and changes nothing;
  - the data root (config, secrets, rules, cursors) is never touched.

  **Skipped:**
  - creating a symbolic link (this account lacks the privilege);
  - the real restricted backup in an NBPL-shaped parent (needs an elevated session).
- **Existing installer tests:** three in `tests/test_collector_build_release.py` that pinned the old bare-copy backup
  and rollback were rewritten for the new behavior.
- **Collector, installer and release suites** (`tests/test_collector_*.py`, `test_installer_*.py`, `test_release_*.py`,
  `test_v2_pilot_release_prep.py`, `test_configure_v2.py`, `test_parser_logging_ownership.py`,
  `test_data_lifecycle_policy.py`, `test_server_containment.py`): **3,238 passed, 62 skipped, 25 failed, 487 errors**
  in 14 min 21 s.
  - **Environmental (24 failures, all 487 errors):** MSBuild MSB3644, because the .NET Framework 4.7.2 reference
    assemblies are not installed on this PC. That is the known gap in the .NET 4.7.2 section above, in the WPF
    installer build tests (`test_installer_*`, unchanged C# code).
  - **Expected until commit (1 failure):** `test_installer_release_packaging.py::test_the_committed_record_is_read_from_head_and_names_the_commit`.
    The packager reads `docs/releases/collector-1.0.15.md` only from `HEAD`, by design. The record is committed as of
    `c1b161e`; this test has not been re-run since.
- `ruff check collector tests src`: clean. `mypy collector/build_release.py collector/__init__.py`: clean.
- All four changed or new PowerShell scripts parse with no errors under Windows PowerShell 5.1.
- **Not run here (need an elevated session or the representative VM):**
  - the real update and restore scripts, with a real task, a real frozen runtime and preflight;
  - elevated restricted-backup creation;
  - symbolic links.

*1.0.15 test additions (2026-11-16, approved):*
- **New elevated test, implemented and NOT yet executed:**
  `test_a_rollback_that_fails_after_moving_the_install_aside_keeps_it_intact_and_collection_disabled`.
  - **Disposable:** the install, backup and config path are under pytest's temporary folder. The Scheduled Task
    cmdlets are replaced by recording stand-ins that report an *enabled* task, and the task name is unique and
    nonexistent.
  - **The failure is real:** the restored runtime cannot run, so preflight fails *after* the original install was moved
    aside and the backup restored.
  - **It requires:**
    - exit code 1;
    - the task disabled and never re-enabled or started;
    - the original install byte-for-byte intact in its single `.replaced-*` folder;
    - the printed recovery commands, run exactly as printed, bringing the original back, with `verify-install.ps1`
      passing on it;
    - the backup kept;
    - the real `C:\SortView` and `C:\ProgramData\SortViewCollector` top levels and ACLs, and the real `SortView
      Collector` task state, unchanged before and after.
- **Changed elevated test, implemented and NOT yet executed:**
  `test_a_backup_made_in_an_nbpl_shaped_parent_is_restricted_owned_and_verified` now also requires that making the
  backup leaves the source installation byte-for-byte unchanged.
- **Fix found while writing the test:** `restore-backup.ps1`'s printed recovery ran `verify-install.ps1` without
  `-InstallRoot`. With a non-default install root it would have verified `C:\SortView\Collector` instead (read-only,
  but the wrong folder). It now passes `-InstallRoot`, and a static test pins this.
- **Executed (non-elevated, development PC LIB-L26, 2026-11-16):**
  - `tests/test_collector_backup_security.py`: **40 passed, 3 skipped**. The 3 skips are the symbolic-link test and the
    two elevated tests; none of the three has been executed.
  - `tests/test_collector_build_release.py`, `test_collector_acl_and_manifest.py` and
    `test_collector_deploy_strictmode_counts.py`: **378 passed, 1 skipped**.
  - `ruff` clean.
- **Executed (elevated), 2026-11-16, on LIB-L26: all three previously skipped tests PASSED, 3 passed in 19.33 s, 0
  failed, 0 skipped.** Run by the product owner in an elevated Windows PowerShell 5.1 administrator session (Python
  3.11.9, pytest 8.4.2), temporary folders only. The tests:
  1. `test_redirect_check_refuses_a_symbolic_link`: a symbolic link is refused as a redirected path.
  2. `test_a_backup_made_in_an_nbpl_shaped_parent_is_restricted_owned_and_verified`: in a parent granting
     Authenticated Users Modify, the backup is SYSTEM+Administrators only on every item and Administrators- or
     SYSTEM-owned. Its copy and recorded files verify, the source install is byte-for-byte unchanged, and the fake data
     root (config, rules, secret, cursor) is unchanged.
  3. `test_a_rollback_that_fails_after_moving_the_install_aside_keeps_it_intact_and_collection_disabled`: a restore
     failing at preflight exits 1. The task is disabled and never re-enabled, the original install is intact in
     `.replaced-*`, the printed recovery restores it and `verify-install.ps1` passes, and the real paths and the real
     task are unchanged.
- **So every test in `tests/test_collector_backup_security.py` has now been executed and passed:** 40 non-elevated and
  3 elevated, on LIB-L26.
- **What these tests do not cover:** a real frozen runtime, a real Scheduled Task, a real 1.0.13 install, or a built
  1.0.15 bundle. Those are the VM rehearsal (blockers 1 and 2).

*Blockers before a production upgrade, open. **The upgrade is not ready until 1 and 2 pass with the 1.0.15 bundle.***
1. **Upgrade rehearsal, 1.0.13 → 1.0.15,** on a VM that reproduces this profile:
   - a 1.0.13 Contract v2 install at the same paths, with parser logs under the install root;
   - the SYSTEM task at run level Highest;
   - the same ACLs, including Authenticated Users Modify on `C:\SortView`.

   Upgrade it with the 1.0.15 bundle's `tools\update.ps1`, then show:
   - the backup is restricted and Administrators-owned;
   - the config, secret, rules and cursor are unchanged;
   - the installed manifest verifies;
   - the SYSTEM preflight passes;
   - a scheduled run uploads with no gap or duplicate.
2. **Rollback rehearsal** with `tools\restore-backup.ps1` to 1.0.13 (anchor: `-TrustedManifestPath` from the
   verified 1.0.13 ZIP). Show that:
   - it refuses an unrestricted, altered, redirected or wrong-anchor backup, changing nothing;
   - it restores a good backup;
   - after a forced failure, the task stays disabled and the printed recovery works.

   Also rehearse restoring 1.0.13 from the canonical ZIP. This is the only rollback path for NBPL's existing
   2026-10-02 backup.
3. **Backup-permissions vulnerability (confirmed 2026-11-16): fix implemented for 1.0.15, not yet released or
   rehearsed.** The fix is to restrict the backup at creation and verify the rollback (above). It is closed when 1 and
   2 pass with the built 1.0.15 bundle.
   - **Elevated security tests: COMPLETE (2026-11-16).** All three passed in an elevated session on LIB-L26 (symbolic
     link, restricted backup, rollback failure recovery); see *1.0.15 test additions* below. The fix is still not
     released or rehearsed, so blocker 3 stays open until 1 and 2 pass.
   - **Still open separately:** the wider `C:\SortView` parent-folder review (profile above).
4. **Canonical 1.0.15 bundle:**
   - [x] build it and record its hashes in `docs/releases/collector-1.0.15.md` -- **done 2026-11-17** (`6b358993…9ec8`);
   - [x] store it in `sortview-artifacts` `collector/1.0.15/` (`docs/release-process.md`) and record the `- Stored:` line
     -- **done 2026-11-17**, commit `09130bde1c45076028482a1fb0d63cbb63873523`;
   - [ ] retrieve it from `sortview-artifacts` on the machine that will carry it to the VM and to NBPL, and verify it
     there;
   - decide whether a 1.0.15 setup program (G2-style) package is needed. NBPL does not need one.
5. Decide which of the release's *Before this release is published* checks gate an NBPL upgrade (those of
   `docs/releases/collector-1.0.14.md` carry over to 1.0.15). The SYSTEM-context preflight is relevant. Resume,
   non-ASCII rules and bootstrap concern fresh installs.
6. After any upgrade, confirm delivery server-side (heartbeat, pipeline health, events arriving), not only
   `LastTaskResult` 0.
7. **Build-environment checks, unresolved:**
   - LIB-L26 lacks the .NET Framework 4.7.2 reference assemblies (MSBuild MSB3644), so the WPF installer build tests
     (`tests/test_installer_*`) error or fail there. Run them on a machine with the targeting pack, or install it, before
     packaging.
   - `test_installer_release_packaging.py::test_the_committed_record_is_read_from_head_and_names_the_commit` passes
     only once `docs/releases/collector-1.0.15.md` is committed. It is committed as of `c1b161e`; the test has not
     been re-run since. It gates only a setup program package, which NBPL does not need (blocker 4).
   - The 1.0.15 bundle must be built on the documented build machine and toolchain, with its hashes recorded.
     **Build machine and toolchain (found 2026-11-16, previously undocumented):** 1.0.14 was built on
     `DESKTOP-DORG9IO` under `C:\SortView\ReleaseBuilds\1.0.14\` with `.pyinstaller-venv` (CPython 3.14.6,
     PyInstaller 6.22.3, hooks-contrib 2026.8; the runtime pins of `docs/releases/collector-1.0.14.md`). That venv
     is unchanged there. The ZIP and extracted-copy verification step (`Compress-Archive` in Windows PowerShell 5.1,
     `Expand-Archive` into `_verify\`, then `version`, every subcommand's `--help`, `install.ps1 -VerifyBundleOnly` and
     a no-extra-files count) exists only as that machine's `_build\package_and_verify.ps1` and
     `BUILD-RECORD.json`, not in this repository. **Not yet in `docs/release-process.md`.**
     **Done 2026-11-17:** 1.0.15 was built there with that toolchain and that script (version string changed only).
     `build_frozen.ps1` needs `-RepoRoot` when run through `powershell.exe -File`: Windows PowerShell 5.1 leaves
     `$PSScriptRoot` empty in `param()` defaults there (the first 1.0.15 attempt stopped before PyInstaller for this).

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
      installation-status linkage. **Installation-status linkage: implemented, merged, deployed, and live-verified
      2026-10-31** (PR #277, merge commit `cc3f1eb`). The backend deployment went live on DigitalOcean at
      12:19:23 PM CDT.

      Contract v2 `/v2/status` heartbeats from installation-bound tokens now update `collector_installations`:
      Provisioning transitions to Active, and `last_seen_at` is refreshed. The implementation preserves existing
      installation authorization, legacy unbound-token behavior, and inactive/retired safeguards. No Collector,
      payload, token, or database schema changes were required.

      **Live validation:** G2 Installation #8 (Contract v2) transitioned from Provisioning to Active following
      a successful Collector run at 12:20 PM CDT on 2026-10-31 (`LastTaskResult: 0`). SortView Administration
      confirmed `last_seen_at` updated to 2026-10-31 12:20 PM CDT while preserving the original installation
      date of 2026-10-29. After validation, the Windows scheduled task was disabled and Installation #8
      deactivated; both cleanup actions were verified.

      **Automated validation:** Targeted and regression tests passed (1,074 passed). The focused PostgreSQL
      lifecycle test (`tests/test_rls_phase1_postgres.py::test_v2_status_keeps_a_bound_installations_lifecycle_as_the_runtime_role`)
      passed locally on PostgreSQL 18 using production-like runtime grants and row-level security. Remaining
      PostgreSQL opt-in tests were not run.

      **Remaining C6 work:** Replacement-machine workflow and key rotation. C6 remains open.

      **Replacement machine (C6a), stage R1:** implemented, validated locally on 2026-11-12, and merged to `main`
      in PR #288 (`d45a2da`).
      - Adds migration `b8e2d4f61a07` (the link to the replaced installation, plus attribution columns that are
        set at authorization and can never change).
      - Adds the admin endpoints `POST …/installations/{id}/replacements` and `…/cancel-replacement`, each
        audited.
      - Every R1 acceptance criterion passed in local validation (local PostgreSQL only):
        - the full PostgreSQL suite: 489 passed, 0 failed;
        - the suite without PostgreSQL: 11,290 passed, 0 failed;
        - covered: audit rollback, the migration round trip, the exact redemption refusal, and the tenant purge of a
          replacement chain.
      - Its migration `b8e2d4f61a07` is applied to production.
      - Details: `docs/c6a-replacement-machine-design.md`, 14.9.

      **Replacement machine (C6a), stage R2 (replacement redemption):** implemented, validated locally on
      2026-11-12, merged to `main` in PR #289 (`e5d24a3`), and deployed (DigitalOcean healthy).
      - Redeeming a replacement code moves the branch's Contract v2 identity to the new computer in one
        transaction: the old computer's tokens, installation and key are retired, and the new one gets a key and
        token. The cutover and stored events are unchanged. The lock order was corrected so it cannot deadlock
        with an upload in progress (design 14.4a).
      - No migration, and no installer or Collector change. It needs R1's migration `b8e2d4f61a07` in production.
      - Every R2 acceptance criterion passed in local validation (local PostgreSQL only):
        - the full PostgreSQL suite: 1,049 passed, 0 failed;
        - the suite without PostgreSQL: 11,320 passed, 0 failed.
      - **Not qualified for customer use.** Since R3, the server-side flag gates replacement, and it is off in
        production. R4 (VM qualification), R5 (commercial gate) and D6 (supervised-pilot use) remain.
      - Details: `docs/c6a-replacement-machine-design.md`, 14.4a and 14.9.

      **Replacement machine (C6a), stage R3 (administration interface, feature flag, runbook):** design approved
      2026-11-12.
      - **Feature flag slice:** `SORTVIEW_MACHINE_REPLACEMENT_ENABLED`, off by default. Merged to `main` in PR #290
        (`0eb6f4d`) after local validation (PostgreSQL suite 1,054 passed; suite without PostgreSQL 11,342 passed;
        0 failed). While off, the backend refuses replacement authorization, code issuance for pending replacements
        and replacement redemption; cancellation and every ordinary operation are unaffected.
      - **Administration interface:** merged in PR #291 (`46ac69a`) and deployed, with the flag off in production.
        "Replace this Collector computer" and "Cancel replacement" on the Installations page, with the installation
        list's new `replaces_installation_id`, `pending_replacement_id` and `replacement_enabled`. Passed full
        local validation on 2026-11-13: PostgreSQL suite 1,060 passed; suite without PostgreSQL 11,348 passed;
        frontend 2,665 passed; 0 failed.
      - **Support runbook:** `docs/c6a-replacement-support-runbook.md`, reviewed against the interface; merged with it.
      - Details: `docs/c6a-replacement-machine-design.md`, 14.11.

      **Replacement machine (C6a), stage R4 (VM qualification):** in progress; **no replacement qualified yet.**
      - **Stage 0 (baseline), passed 2026-11-13:**
        - the G2 package hash matches the release record;
        - production shows the flag off and no Replace button;
        - installation `9` is Active and healthy;
        - a clean Windows snapshot is available;
        - a read-only production query recorded the source's key, token, cutover, and 13 events with their
          fingerprints, which a replacement must leave unchanged.
      - **Stage 1 (isolated staging rehearsal):** proposed, not started.
      - **Product-owner decision:** whether the production qualification may turn the flag on for one supervised
        session, or whether staging evidence is accepted instead.
      - Details: `docs/c6a-r4-qualification.md`; design 14.9.
- [ ] **C7. Self-serve commercial flow** (Stripe against the existing plans/subscriptions schema; guided signup) —
      last, and only once a second customer is real. Includes deciding customer-visible plan/subscription display
      (deferred here by D8).
- [ ] **C8. Multi-branch / multi-site provisioning** (assigned by A1b decision 5, 2026-10-14) — adding non-primary
      branches and sorter sites; no code path exists today. Not built in A2.

**Exit:** a new library goes from signed download to "ready" with no one on site.

---

## 5. Done recently (newest first)

- 2026-11-17 — UI2 (priority 2), Live Today "Sorter at a glance" reduced to the sort-line diagram alone. **Implemented
  on branch `feature/ui2-sorter-diagram-focus` (from `main` at `d2b36de`); not committed, merged or deployed.**
  - **Removed:** the visible header and caption, the Induction, Routed and Holds panels, the "Sort line" label and the
    shading legend. Those figures remain in Today's figures and Holds today. The panel's name stays a visually hidden
    heading for screen readers.
  - **Changed:** the line now fills the panel, centred, capped per column by `--columns` so a short machine is not
    stretched. Bins, belt, labels, counts and fills are larger.
  - **Unchanged:** bin order, layout mapping, heat and fill calculations, colours, the narrow-screen grid, and
    backend/API.
  - **Tests:** frontend lint, both type-checked production builds and Vitest (57 files, 2,730 tests) pass. Not yet
    checked visually in a browser.

- 2026-11-17 — Collector 1.0.15 canonical ZIP **stored** in `sortview-artifacts` (`collector/1.0.15/`, with its
  `ARTIFACT-RECORD.md`), commit `09130bde1c45076028482a1fb0d63cbb63873523` on `origin/main`. The ZIP read from that
  commit matches the release record (SHA-256 `6b358993…9ec8`, 36,555,502 bytes). Release record updated with the
  `- Stored:` line.
  - **Still not production-ready.** Open: retrieval and verification on the machine that carries it to the VM, the
    1.0.13 → 1.0.15 upgrade rehearsal and the rollback rehearsal (section 4, C, blockers 1, 2 and 4). NBPL stays on
    1.0.13.

- 2026-11-17 — Collector 1.0.15 canonical bundle **built and verified** on `DESKTOP-DORG9IO` from `2565b95` (same
  toolchain and procedure as 1.0.14): ZIP SHA-256 `6b3589938b5c255dd74e2a89537a9e1c1db63d84728f605e7e8495789dc59ec8`,
  `MANIFEST.json` SHA-256 `50eac4ca11675debf470d928fdfb664e950199d0c74808c22e47d37dab2eddab`, executable SHA-256
  `6b0f568b…77a3`, 1394 entries.
  - The extracted copy matched its manifest (0 missing, 0 extra). The four backup-security scripts match source and
    manifest. The secrets and library-data inspection found nothing.
  - **Not stored, not rehearsed, not production-ready.** Next: store it in `sortview-artifacts`, then the 1.0.13 →
    1.0.15 upgrade and rollback rehearsal on a representative VM (section 4, C, blockers 1 and 2). NBPL stays on 1.0.13.

- 2026-11-16 — Collector 1.0.15 backup security **merged to `main`** (PR #303, merge commit `c1b161e`). The entries
  below describe the working tree before that merge. Merged source is not a release: **no 1.0.15 bundle is built,
  hashed, stored or rehearsed**, and NBPL stays on 1.0.13. Release readiness for 1.0.15 passed at `c1b161e` on
  `DESKTOP-DORG9IO`; the build machine and toolchain are recorded under section 4, C, blocker 7.

- 2026-11-16 — Collector 1.0.15 elevated security tests **passed** on LIB-L26 (elevated Windows PowerShell 5.1,
  Python 3.11.9, pytest 8.4.2): 3 passed in 19.33 s, 0 failed, 0 skipped. Those were the symbolic-link refusal, the
  restricted backup in an NBPL-shaped parent, and the rollback failure recovery.
  - The whole backup-security test file has now passed: 40 non-elevated and 3 elevated.
  - **1.0.15 is not production-ready.** Still to do: the 1.0.13 → 1.0.15 upgrade and rollback rehearsal on a
    representative VM, building the 1.0.15 bundle, recording and verifying its hashes, and the build-environment
    checks (section 4, C).

- 2026-11-16 — Collector 1.0.15 tests extended (not committed):
  - a new elevated rollback failure-recovery test, and a source-unchanged assertion in the elevated backup test. Both
    are **implemented, not executed**;
  - a fix so the restore's printed `verify-install.ps1` step targets the right install.
  - Non-elevated run: 40 passed, 3 skipped (the elevated and symbolic-link tests).
  - Next: run the 3 in an elevated session on LIB-L26, then the VM upgrade and rollback rehearsal (section 4, C).

- 2026-11-16 — Collector 1.0.15 backup security **implemented on the working tree (not committed, built or released)**.
  - `tools\update.ps1` creates its backup restricted, refuses pre-existing or redirected paths, and verifies the copy
    (ACL, owner, hashes) before replacing the install.
  - New `tools\restore-backup.ps1` restores only a restricted backup matching a trusted `MANIFEST.json` hash obtained
    outside it.
  - New tests: 40 passed, 2 skipped (they need elevation or the symbolic-link privilege). The broader suite's failures
    are the known missing .NET 4.7.2 targeting pack, plus one packaging test that passes once the 1.0.15 release
    record is committed.
  - **NBPL stays on 1.0.13. The upgrade is not ready** until a real 1.0.13 → 1.0.15 upgrade and rollback rehearsal
    passes (section 4, C).

- 2026-11-16 — NBPL profile, second capture: `C:\ProgramData\SortViewCollector\data`, `logs` and `secrets` are
  Administrators and SYSTEM only. The 2026-10-02 backup inherits Authenticated Users Modify from `C:\SortView`. This is
  now a **confirmed backup-permissions vulnerability** (no tampering observed): `tools\update.ps1` creates every backup
  that way. It is recorded as 1.0.14 upgrade blocker 3, and the proposed fix is in Parked. No installer change made;
  production unchanged.

- 2026-11-16 — NBPL production sorter profile consolidated (section 4, C, *Canonical NBPL production sorter
  profile*). It records the product owner's read-only checks: installed bundle 1.0.13 (built 2026-10-02T00:33:47Z,
  installed 2026-10-02T13:18:55Z), the SYSTEM task at run level Highest with last result `0` at 2:12:12 PM, the data
  root `C:\ProgramData\SortViewCollector`, and ACLs. The install folder, executable, data root and config are
  Administrators and SYSTEM only. `C:\SortView` gives Authenticated Users Modify, an **open** security concern.
  The 1.0.14 upgrade status was reconciled: the artifacts are stored and hash-recorded. **No 1.0.13 → 1.0.14 upgrade
  or rollback test exists**; every 1.0.14 validation was a fresh install. The G2 setup program cannot upgrade NBPL;
  `tools\update.ps1` is the path. Its backup is created unhardened under `C:\SortView`. Production unchanged.

- 2026-11-16 — UI2 v7, full-history Holds report: **implemented on branch `feature/ui2-v7-historical-holds`, not
  merged or deployed.** The 92-day holds-only limit is removed (it was technical, not a plan limit; NBPL's plan already
  allows 3,650 days). Holds are now the sum of daily counts (each item at most once a day, by its latest record that
  day), read 31 days at a time; a one-day range equals Live Today's "Holds today" (Live Today unchanged). Legacy days
  take each patron's latest record from a fixed 180-day lookback (an internal classification window, not a report
  limit), read once and carried forward so no row is read twice. The API adds `days_with_data`, and the page explains
  the counting, the coverage and the pre-Contract-2 limits. **Behaviour change:** totals for multi-day ranges change wherever an item was
  held on more than one day.
  **NBPL production coverage (read-only Neon SQL Editor queries, 2026-11-16):** NBPL is operational customer 1,
  branch 1 ("Main AMH Sorter"); cutover 2026-09-28 18:03:41 Chicago time. Legacy `acs_events`: 2026-03-31 to the cutover,
  177 days with records, 43,602 hold records. Contract 2 `acs_item_events`: from 2026-08-31 (parallel running before
  the cutover, ignored by the report) to today. No gaps of 7+ days. Legacy holds with a patron id (35,605): patron
  record same day 52.8%, 1–31 days earlier 22.1%, more than 31 days 3.7% (1,313), none before 21.4% (7,613); longest
  gap 175 days. **Decided 2026-11-16:** `V1_PATRON_LOOKBACK` is 180 days, covering every observed case. Still
  unclassifiable either way: the 7,613 legacy holds with no earlier patron record (counted public; stated on the page).
- 2026-11-12 — G2 fresh installation and end-to-end ingestion passed on a clean VM (installation `9`, library
  `sortview-g2-qualification-2`, left active); PostgreSQL holds 13 events, including 2 from a corrected test-harness
  error. Recorded in `docs/releases/collector-1.0.14.md` (section 4, C).

- 2026-10-29 — G2 exact-package GUI installation passed on a VM (installation `8`, deactivated afterwards); automatic
  task run passed only after a trigger edit. Recorded in `docs/releases/collector-1.0.14.md` (section 4, C).

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
- 2026-11-16 — Bin-layout management UI (UI2 v6 follow-up): a sorter's `bin_layout` (`branch_settings.settings_json`) can only be set by an operator with `scripts/set_sorter_bin_layout.py` (procedure: `docs/sorter-bin-layout-procedure.md`). A Super Admin / React admin screen to view, set and clear a sorter's layout, reusing `services.bin_layout` validation, writing only that key, with an audit of who changed it. Also undecided: sorters with an odd number of bins along the belt (a layout column is exactly two bins today).
- 2026-11-16 — **Approved and implemented the same day for 1.0.15 (section 4, C, upgrade blocker 3); kept here as the
  record of the proposal.** The implementation also creates the backup folder already restricted (closing the moment
  between creating and restricting it) and re-owns every backup item to Administrators.
  Original proposal: **restricted upgrade backups and a verified rollback.**
  - **In `collector/deploy/update-release.ps1` step 2:**
    - refuse if the backup path already exists or is a reparse point;
    - create it empty and `Protect-CollectorPath` it *before* copying, so every copied child inherits Administrators
      and SYSTEM only from the moment it exists;
    - copy the install root's contents into it;
    - then re-verify with `Test-CollectorPathProtected -Recurse` and `Test-InstalledManifest -InstallRoot <backup>`
      before the install root is removed;
    - harden the source-mode `…-full` path after its `Move-Item` the same way.
  - **New `tools\restore-backup.ps1`**, replacing the printed manual `Copy-Item` rollback. It requires elevation and a
    disabled task, then:
    - refuses reparse points and refuses a backup that is not exactly protected;
    - runs `Test-InstalledManifest` on the backup, plus an optional operator-supplied expected `MANIFEST.json` SHA-256
      from the release record;
    - copies into a fresh, protected install root and runs `Test-InstalledManifest` again on the result, then
      preflight;
    - re-enables the task only after all of that passes.
  - **Tests:** static order checks in `tests/test_collector_build_release.py` and
    `tests/test_collector_acl_and_manifest.py`, plus PowerShell-gated behavioral tests (a parent folder granting
    Authenticated Users Modify still yields an exactly protected backup; restore refuses an unprotected, tampered or
    junctioned backup).
  - **Needs:** a new canonical bundle and its qualification.
- 2026-11-16 — `tools\update.ps1`, **source-mode** full rebuild only (requirements changed): after the install is moved
  aside to `…-full`, the new install root is recreated with `New-Item -Force` in the (possibly user-writable) parent,
  so a folder planted there would be reused. The frozen path (all customer installs, NBPL included) was fixed in
  1.0.15; the source path is developer/QA only. Use `New-ProtectedCollectorDirectory` there too if source installs
  ever reach a customer.
- 2026-11-17 — **Frontend CI `npm audit --audit-level=high` fails on every branch, `main` included.**
  - **Cause:** picomatch GHSA-fc5q-39xc-vjpf (high, ReDoS via crafted extglobs), advisory updated 2026-11-17.
    **No patched release exists**: picomatch 4.0.7 is both the latest and the last vulnerable version.
  - **Exposure:** dev-only. picomatch reaches the frontend only through build and test tools (Vite, Vitest,
    tinyglobby/fdir, typescript-eslint), with glob patterns from our own config. `npm audit --omit=dev` finds 0
    vulnerabilities, so it is not in the shipped app.
  - **Fixed on `feature/ui2-sorter-diagram-focus`:** in-range lockfile updates (ESLint 10.12.0 → 10.15.0,
    typescript-eslint 8.71.1 → 8.73.0) remove minimatch GHSA-g3hv-75wg-m249 (moderate) everywhere except
    `eslint-plugin-jsx-a11y-x` 0.2.0. That is the plugin's latest version and it still requires minimatch `^10`.
  - **Remaining:** 14 findings (2 moderate, 12 high), all from those two advisories. npm's only offered "fixes" are
    downgrades (Vite 6.2.7, Vitest 3.1.4, jsx-a11y-x 0.1.1): rejected.
  - **Decided and implemented 2026-11-17 (product owner): a temporary CI exception for GHSA-fc5q-39xc-vjpf only.** On
    `feature/ui2-sorter-diagram-focus`; not committed or merged.
    - **How it works:** `frontend/scripts/audit-gate.mjs` replaces `npm audit --audit-level=high` in
      `.github/workflows/frontend-ci.yml`. It allows only picomatch `<=4.0.7`, until **2026-12-17** (UTC), and only
      while every package the advisory reaches has `dev: true` in `package-lock.json`.
    - **It fails on:**
      - any other high or critical advisory;
      - the advisory reaching a production or optional dependency;
      - a change to the advisory's package or range;
      - a published picomatch release outside `<=4.0.7`;
      - expiry;
      - any `npm audit`, `npm view`, lockfile or output failure, or incomplete advisory data (fails closed).
    - **Tests:** 49 in `frontend/scripts/audit-gate.test.mjs`.
    - **On the current tree it passes:** the high advisory is allowed, development-only, across 12 packages; the
      moderate minimatch advisory is listed but does not block. Plain `npm audit --audit-level=high` still exits 1
      (14 findings: 2 moderate, 12 high).
    - **To remove:** when a fixed picomatch is published (the gate itself then fails), update the lockfile to it, delete
      the script and test, and restore the workflow step to `npm audit --audit-level=high`. Whatever happens, act by
      2026-12-17.

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
