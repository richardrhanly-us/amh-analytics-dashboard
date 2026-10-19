# SortView React Accessibility Verification — 2026-10-19

## Purpose

This record documents accessibility evaluation performed against the production
SortView React customer and administration applications as part of government
readiness workstream B2.

Applications evaluated:

- Customer application: https://app.sortviewapp.com
- Administration application: https://admin.sortviewapp.com

The evaluation was performed against the React applications that replaced the
retired Streamlit customer interface.

This document records the checks actually performed. It is not, by itself, a
claim of complete WCAG 2.1 Level AA conformance and is not a completed VPAT or
Accessibility Conformance Report (ACR).

## Automated accessibility baseline

The frontend includes automated accessibility checks in the normal development
and CI path.

Implemented controls include:

- axe-core automated accessibility testing
- jsx-a11y lint rules through eslint-plugin-jsx-a11y-x
- representative customer and administration axe tests
- a control test proving that the axe test harness detects real violations
- automated tests for skip-link behavior
- automated tests for route-arrival focus behavior
- automated tests for accessible form guidance and validation
- automated tests for breadcrumb semantics and navigation behavior

Following the B2c admin breadcrumb change:

- full frontend test suite: 2,615 / 2,615 passed
- customer accessibility/axe tests: 40 / 40 passed
- representative admin axe, focus and breadcrumb tests: 26 / 26 passed
- lint: passed
- npm audit: 0 vulnerabilities
- customer production build: passed
- administration production build: passed
- frontend boundary/scope checks: passed

## Manual production verification

Manual checks were performed against the production customer and administration
applications.

### Keyboard access and focus

Customer and administration workflows were traversed using keyboard navigation.

Verified:

- interactive controls could be reached using Tab and Shift+Tab
- controls could be operated from the keyboard
- visible keyboard focus remained apparent
- no keyboard focus traps were encountered
- administration navigation included Libraries, library overview, Collector
  configuration, Installations, Provision a library and Log out

Result:

- WCAG 2.1.1 Keyboard — customer: pass on tested workflows
- WCAG 2.1.1 Keyboard — administration: pass on tested workflows
- WCAG 2.4.7 Focus Visible — customer: pass on tested workflows
- WCAG 2.4.7 Focus Visible — administration: pass on tested workflows

### Resize and reflow

Representative customer and administration pages were tested at 200% browser
zoom.

No loss of content, overlapping content or unusable controls was observed.

Representative pages were also tested at a 320 x 800 CSS-pixel viewport.
Content reflowed without whole-page horizontal scrolling. Horizontal scrolling
was limited to genuine data-table regions where appropriate.

Result:

- WCAG 1.4.4 Resize Text — customer: pass on tested pages
- WCAG 1.4.4 Resize Text — administration: pass on tested pages
- WCAG 1.4.10 Reflow — customer: pass on tested pages
- WCAG 1.4.10 Reflow — administration: pass on tested pages

### Text spacing

The following temporary browser CSS override was applied during manual testing:

    * {
      line-height: 1.5 !important;
      letter-spacing: 0.12em !important;
      word-spacing: 0.16em !important;
    }

    p {
      margin-bottom: 2em !important;
    }

Representative customer and administration pages remained readable and usable
without clipped text, overlapping content or inaccessible controls.

Result:

- WCAG 1.4.12 Text Spacing — customer: pass on tested pages
- WCAG 1.4.12 Text Spacing — administration: pass on tested pages

### Contrast and forced-colors behavior

Static application color combinations were reviewed during the accessibility
audit.

Windows contrast mode was also enabled and representative customer and
administration pages were manually inspected.

Text, controls, control boundaries, focus indication and status information
remained visible and understandable.

Result:

- WCAG 1.4.3 Contrast (Minimum) — pass on tested content
- WCAG 1.4.11 Non-text Contrast — pass on tested controls and states

### Screen reader verification

NVDA was used for production checks.

Customer checks included:

- sign-in and keyboard traversal
- Live Today headings and controls
- chart/table presentation
- table headers and cells
- Routing validation error announcement
- successful Routing save announcement

Administration checks included:

- navigation between Libraries, library overview and Collector configuration
- route-arrival focus
- heading context
- sign-in/sign-out navigation behavior
- breadcrumb navigation

Observed results included meaningful names and roles for tested controls,
understandable table relationships, announced validation errors and successful
save status messages.

Result on tested workflows:

- WCAG 1.1.1 Non-text Content — customer chart/table presentation: pass
- WCAG 1.3.1 Info and Relationships — tested customer table: pass
- WCAG 2.4.3 Focus Order — administration: pass
- WCAG 3.3.1 Error Identification — customer Routing validation: pass
- WCAG 4.1.2 Name, Role, Value — tested customer and administration flows: pass
- WCAG 4.1.3 Status Messages — tested customer error and successful-save flow: pass

### Headings and page structure

NVDA heading navigation was used on representative administration views.

Heading levels followed a meaningful hierarchy and could be traversed
successfully.

Reading order was also reviewed on representative customer and administration
pages. The programmatic/read order followed the intended content order.

Result:

- WCAG 1.3.2 Meaningful Sequence — customer: pass on tested pages
- WCAG 1.3.2 Meaningful Sequence — administration: pass on tested pages
- WCAG 2.4.6 Headings and Labels — administration: pass on tested pages

### Navigation

The customer application provides breadcrumb and local navigation appropriate to
its hierarchy.

Administration navigation was improved during B2c to provide semantic
breadcrumb navigation on views below the Libraries root.

Example production breadcrumb:

    Libraries / New Braunfels Public Library / Collector configuration

The breadcrumb uses a named navigation landmark, ordered-list structure,
ancestor links and a non-link current-page indicator.

The B2c administration build was deployed to production and manually verified
on 2026-10-19.

Result:

- WCAG 2.4.5 Multiple Ways — customer: pass
- WCAG 2.4.5 Multiple Ways — administration: pass

### Input behavior and consistent identification

Customer report date/range controls were exercised manually. Changing input
values did not unexpectedly navigate away or cause an unrelated change of
context.

Repeated actions and destinations were spot-checked in both applications and
were consistently identified within their respective application contexts.

The customer application uses "Sign out" while the administration application
uses "Log out"; these labels are internally consistent within their separate
application contexts.

Result:

- WCAG 3.2.2 On Input — customer report controls: pass
- WCAG 3.2.4 Consistent Identification — customer: pass
- WCAG 3.2.4 Consistent Identification — administration: pass

### Sensory characteristics

Representative instructions and controls were reviewed for directions that
depended solely on visual location, shape, size or color.

No sensory-only instructions were identified. Controls and instructions use
textual names such as "Apply dates", "Pause automatic refresh" and
"Provision a library".

Result:

- WCAG 1.3.3 Sensory Characteristics — customer: pass
- WCAG 1.3.3 Sensory Characteristics — administration: pass

## Remediation completed during B2

Accessibility work completed during the React review included:

- automated axe accessibility baseline
- blocking JSX accessibility linting
- customer and administration skip links
- route-arrival focus handling
- per-view document titles
- required-field guidance and native required semantics
- validation and status-message coverage
- administration breadcrumb navigation
- tests protecting focus, accessibility semantics and navigation behavior

## Interpretation and limits

The evidence above supports the accessibility behavior actually tested on the
production SortView React applications.

It does not establish that every possible state, route, browser, assistive
technology combination or WCAG 2.1 Level AA success criterion has been
independently evaluated.

Public and procurement-facing wording should therefore avoid statements such as
"fully WCAG 2.1 AA compliant" unless a complete conformance determination has
been performed.

Appropriate interim wording is that SortView is designed toward WCAG 2.1 Level
AA and uses automated accessibility checks together with manual keyboard,
screen-reader, reflow, contrast and interaction testing.

A formal Accessibility Conformance Report / VPAT may use this record as
supporting evidence but should make its own criterion-by-criterion
Supports / Partially Supports / Does Not Support / Not Applicable
determinations.
