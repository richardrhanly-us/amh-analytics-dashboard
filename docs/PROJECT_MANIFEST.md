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
Super Admin (internal staff tool) may stay on Streamlit until later.

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
  - Director PDF export (Streamlit `reports_labor.py`) — React has no PDF export.
  - Internal-routing lists (Branch/Collection Services names, patron account names) — editable only in Streamlit
    Admin Settings. Decide: React (owners/admins), Super Admin only, or Collector-side only.
  - Streamlit admin-password lock (`security` settings) — likely obsolete under React's roles; decide.
  - Streamlit Transits view and Overview (internal workflow) — confirm React routing/holds reports cover them.
  - Demo/guest mode — decide whether it survives.
- [ ] **A2. Close parity gaps** decided in A1 (each its own small PR).
- [ ] **A3. Production hosting:** CloudFront (per runbook) on a SortView custom domain, TLS, production API origin.
- [ ] **A4. Cutover:** NBPL moved to React; Streamlit customer access removed or redirected.
- [ ] **A5. Retire** Streamlit customer code paths no longer reachable.

**Exit:** no customer signs in to Streamlit.

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
      last, and only once a second customer is real.

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
