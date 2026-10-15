# SortView project manifest

The big picture, kept in one place so a single task does not swallow the plan. Read this before starting any
non-trivial task, and check the task against it before widening scope.

Last reviewed: 2026-10-14. Facts marked **(verify)** were not confirmed against production or the person who
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

| Order | Workstream | Why it goes here |
|---|---|---|
| 0 | **Land in-flight work** | Finished branches that are not merged are the main source of drift. Close them first. |
| 1 | **A. Streamlit → React cutover** | Government readiness and customer onboarding both target *the customer UI*. Today that is Streamlit Community Cloud; tomorrow it is React. Anything done to the UI surface before the cutover is done twice. |
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

**Phase 1 / Workstream A, milestone A1 (parity checklist).**

Phase 0 (completed 2026-10-14):

- [x] `fix/v1-first-ingest-readiness` — legacy uploads make a pending branch ready. Merged (PR #190).
- [x] React "Waiting for first data" UI — merged (PR #191).

---

## 4. Workstreams

### A. Streamlit → React cutover

**Goal:** every customer uses the React app on a SortView domain; the Streamlit customer dashboard is retired.
The migration also includes the internal provisioning and library-management surface SortView needs to operate
(today the Super Admin Streamlit app). Super Admin stays on Streamlit only until its React replacement is ready; its
exact scope is set by A1b.

**Where it stands (verified):**

- Current production customer UI: Streamlit Community Cloud, `https://sortview.streamlit.app/`.
- React customer app (`frontend/`) is already substantial and functional: Organizations, Organization page,
  Organization Reports (operations, value), Members, Account (including password reset), Settings (general,
  branches, routing, efficiency), Sorter Live Today, Sorter Reports (operations, efficiency). Keyboard and
  screen-reader behavior is tested (`frontend/src/accessibility.test.tsx`).
- React is hosted through CloudFront for trial/testing only (`docs/react-hosting-runbook.md`). It is **not yet the
  production customer entry point**; `docs/deployment.md` still says the React app is not deployed.
- Recent "decoupling" slices extracted shared policy out of Streamlit (pipeline rules, access mode, settings
  merge, readiness, onboarding status).

**Milestones:**

- [ ] **A1. Parity checklist.** A written list of every customer-facing Streamlit capability, each marked
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
    owner: the future React SortView administration/provisioning surface (scoped by A1b); Super Admin already
    collects them at provisioning (`super_admin/pages/Provision_Library.py`). The existing Streamlit paths stay
    until that replacement is ready. Known limit: hosted edits do not update an installed v2 collector's
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
- [ ] **A1b. Super Admin parity audit** (read-only; immediately after D9, before any A2 work). Inventory every
      Super Admin capability needed to provision and manage libraries, classify each as migrate / drop /
      redesign, and set the React administration scope. D6 shows at least the Internal Routing lists belong there;
      do not assume that is the whole list.
  - Must also decide (from D9): the customer-accessible `GET …/ingest-status` route returns the full v2 heartbeat to
    any organization member, and React deliberately never calls it. Move it behind internal/admin/support
    authorization, or retire it.
- [ ] **A2. Close parity gaps** decided in A1 and A1b (each its own small PR). Starts after A1b.
- [ ] **A3. Production hosting:** CloudFront (per runbook) on a SortView custom domain, TLS, production API origin.
- [ ] **A4. Cutover:** NBPL moved to React; Streamlit customer access removed or redirected.
- [ ] **A5. Retire** Streamlit customer code paths no longer reachable.
  - Admin-lock cleanup (added 2026-10-14, D2): inventory and remove legacy `security` settings; handle any
    remaining plaintext `security.admin_password` through the existing approved process
    (`docs/production-verification-runbook.md` Part 1); retire `admin_lock_service.py`, its scripts, tests and
    the `docs/deployment.md` section with the Streamlit code.
  - Legacy guest cleanup (added 2026-10-14, D7): remove the README `?guest=1` demo link; remove the
    `SORTVIEW_DEMO_MODE_ENABLED` / `SORTVIEW_GUEST_EMAIL` / `SORTVIEW_GUEST_PASSWORD` settings (and their
    `docs/deployment.md` rows) when the Streamlit guest path is retired; deactivate or remove the unused
    `guest@gmail.com` account through a supported process if it has no remaining purpose.

**Exit:** no customer signs in to Streamlit, and the provisioning/library-management functions A1b finds necessary
have a supported React replacement, so the Super Admin Streamlit app can be retired (milestone wording refined
after A1b).

### B. Government readiness

**Goal:** SortView can pass a city/government IT, security, privacy and accessibility review, with written
evidence for each answer. The original "government-readiness audit" (Parts 1–7, referenced in code comments)
covered Contract v2 dashboard wiring, mixed-era reads and release packaging; that round is complete. This section
is the current gap list, rebuilt from the repository on 2026-10-14.

**Done (verified in the repository):**

- Contract v2 is live at NBPL.
  - Intended behavior (by design, not a verified production claim): Contract v2 filters raw patron data on the AMH
    machine before upload.
  - **(verify)** The exact PII behavior of the collector currently deployed in production.
- Row level security on operational event tables; tenant isolation tested.
- Rate limits on uploads, enrollment, login, password change and reset; CORS and security headers on the API.
- HttpOnly, SameSite=Lax session cookies for the API; self-service password reset; lockouts; auth audit log.
- Guest-credential fallback removed (demo mode explicitly gated).
- Sentry error tracking with privacy scrubbing (`services/privacy_hardening.py`).
- CI: pytest, ruff, mypy, bandit, pip-audit, Dependabot, frontend test/lint/build; uptime and pipeline-health checks.
- Backup/restore runbook; data lifecycle and offboarding (doc, `tenant_lifecycle_events`, Super Admin offboarding).
- Collector install/update/repair scripts apply least-privilege ACLs (`collector/deploy/CollectorAcl.ps1`).
- Proprietary LICENSE.
- City data-access rule enforced in design: city machines reach data only through the API.

**Gaps:**

| # | Gap | Notes | Depends on |
|---|---|---|---|
| B1 | Customer UI hosted on Streamlit Community Cloud | Shared third-party host, no SortView domain. Closed by A. | A |
| B2 | No accessibility conformance evidence | Accessibility fixes so far were on **Streamlit** (contrast, KPI semantics, charts/forms/admin, status announcements, tables: PRs #72–#74, #124–#127, 2026-09-22 to 10-03). React has keyboard/screen-reader tests but no automated a11y checks (axe), no WCAG 2.1 AA audit and no ACR/VPAT. U.S. state/local governments must meet WCAG 2.1 AA under the ADA Title II web rule. Do the audit on React. | A |
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

**Where it stands (verified):**

- Collector 1.0.14. Server-enforced enrollment (stage A1), fresh-install bootstrap and preflight (A2), fresh
  Contract v2 installation (A3a), Contract v2 as the provisioning default (A3b).
- Install, update, uninstall, repair and verify scripts in `collector/deploy/`; guided `setup-collector.ps1`.
- First ingest moves a branch from `pending` to `ready`, on both contracts (PRs #183, #190).

**Milestones:**

- [ ] **C1. Code signing** for the Collector executable and scripts (needs a certificate — section 6).
- [ ] **C2. One signed download** a library can run, replacing hand-copied bundles.
- [ ] **C3. First-owner account at provisioning** — today no code path creates a new library's first login
      (memberships can only be added by an existing admin).
- [ ] **C4. Current customer install guide** — the published guide is for Collector 1.0.2.
- [ ] **C5. Decide the legacy contract's future** — Super Admin still offers Contract 1 for new installs.
- [ ] **C6. Deferred Collector work** from `docs/collector-v2.md`: replacement-machine workflow, key rotation,
      installation-status linkage.
- [ ] **C7. Self-serve commercial flow** (Stripe against the existing plans/subscriptions schema; guided signup) —
      last, and only once a second customer is real. Includes deciding customer-visible plan/subscription display
      (deferred here by D8).

**Exit:** a new library goes from signed download to "ready" with no one on site.

---

## 5. Done recently (newest first)

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
- SortView domain and DNS (A3).
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
- 2026-10-14 — `app.py` checks operational mapping twice (readiness gate and line ~652).
- 2026-10-14 — Two stale unmerged branches from 2026-09-25, `fix/live-today-controls-layout` and `fix/live-today-spacing`: decide merge, rebase or delete. Not a blocker for A1.
- 2026-10-01 — `ruleset_id`: `docs/contract-v2-design.md` §5 contradicts the deliberate code behavior; fix the doc or the code.

---

## Change log

- 2026-10-14 — Manifest created. Order set to: Phase 0 → A (React cutover) → B (government readiness) → C (installer
  commercialization), with long-lead items in parallel.
- 2026-10-14 — Workstream A boundary widened (product owner, prompted by D6): the React migration includes the
  internal provisioning/library-management surface; Super Admin may no longer stay on Streamlit indefinitely. Added
  A1b (Super Admin parity audit after D9) as a prerequisite to A2. Workstream order unchanged.
