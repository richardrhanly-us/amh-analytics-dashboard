# SortView Accessibility Conformance Report

## Product

SortView React customer application and SortView React administration application.

Production applications:

- Customer: https://app.sortviewapp.com
- Administration: https://admin.sortviewapp.com

## Evaluation basis

This report evaluates the production React applications against WCAG 2.1
Level A and Level AA success criteria.

Supporting evidence includes:

- `docs/accessibility-verification-2026-10-19.md`
- automated axe-core accessibility testing
- blocking JSX accessibility linting
- automated focus, form, navigation and breadcrumb tests
- manual keyboard testing
- manual 200% zoom and 320 CSS-pixel reflow testing
- manual text-spacing testing
- manual Windows contrast-mode testing
- manual NVDA screen-reader testing

## Conformance terminology

The following terms are used in this report:

- **Supports** — the tested implementation meets the applicable success criterion,
  and available code/test/manual evidence does not identify a known exception.
- **Partially Supports** — some functionality meets the criterion, but an identified
  limitation, untested material surface, or known exception prevents a full
  Supports determination.
- **Does Not Support** — the implementation does not meet the criterion.
- **Not Applicable** — the success criterion does not apply to the evaluated product.

A **Supports** determination in this document is limited to the product and
evidence evaluated. It is not a claim that every browser, assistive technology,
route, state, or future product version has independently been tested.

## WCAG 2.1 Level A and AA

| Criterion | Conformance | Remarks and Explanations |
|---|---|---|
| 1.1.1 Non-text Content (Level A) | Supports | The customer Live Today chart/table presentation was manually verified with NVDA, and representative customer/admin states are covered by axe testing. No known non-text-content exception was identified in the evaluated React applications. |
| 1.2.1 Audio-only and Video-only (Prerecorded) (Level A) | Not Applicable | The evaluated SortView React applications do not provide prerecorded audio-only or video-only content. |
| 1.2.2 Captions (Prerecorded) (Level A) | Not Applicable | The evaluated applications do not provide prerecorded synchronized media requiring captions. |
| 1.2.3 Audio Description or Media Alternative (Prerecorded) (Level A) | Not Applicable | The evaluated applications do not provide prerecorded synchronized media requiring audio description or a media alternative. |
| 1.2.4 Captions (Live) (Level AA) | Not Applicable | The evaluated applications do not provide live synchronized media. |
| 1.2.5 Audio Description (Prerecorded) (Level AA) | Not Applicable | The evaluated applications do not provide prerecorded video requiring audio description. |
| 1.3.1 Info and Relationships (Level A) | Partially Supports | Semantic relationships are covered by automated accessibility tests, and the customer Live Today data table was manually verified with NVDA for understandable headers and cell relationships. Additional customer and administration data/form states were not exhaustively verified with assistive technology, so this remains Partially Supports pending broader criterion-level review. |
| 1.3.2 Meaningful Sequence (Level A) | Supports | Representative customer and administration pages were manually reviewed for reading order, and the programmatic/read sequence followed the intended content order. No known exception was identified in the evaluated React applications. |
| 1.3.3 Sensory Characteristics (Level A) | Supports | Representative instructions and controls were reviewed for dependence on visual location, shape, size, or color. No sensory-only instructions were identified; tested controls and instructions use textual labels. |
| 1.3.4 Orientation (Level AA) | Supports | The evaluated React applications do not restrict content to a single display orientation. No orientation-dependent requirement was identified. |
| 1.3.5 Identify Input Purpose (Level AA) | Partially Supports | Forms use semantic labels and native input types where implemented, and required-field behavior was remediated during B2. A complete review of autocomplete/input-purpose tokens across every user-data field has not yet been performed. |
| 1.4.1 Use of Color (Level A) | Partially Supports | Representative pages and controls were reviewed in normal and Windows contrast modes, and tested information remained understandable. A complete criterion-specific review of every status, chart, and validation state for color-only communication has not yet been performed. |
| 1.4.2 Audio Control (Level A) | Not Applicable | The evaluated SortView React applications do not automatically play audio. |
| 1.4.3 Contrast (Minimum) (Level AA) | Supports | Static application color combinations were reviewed during the accessibility audit, and representative customer and administration pages were manually verified in Windows contrast mode. No known text-contrast exception was identified in the evaluated content. |
| 1.4.4 Resize Text (Level AA) | Supports | Representative customer and administration pages were manually tested at 200% browser zoom without loss of content, overlapping content, or unusable controls. |
| 1.4.5 Images of Text (Level AA) | Not Applicable | The evaluated React applications use rendered text for application content and controls. No required image-of-text content was identified in the evaluated interfaces. |
| 1.4.10 Reflow (Level AA) | Supports | Representative customer and administration pages were tested at a 320 x 800 CSS-pixel viewport without whole-page horizontal scrolling. Horizontal scrolling was limited to genuine data-table regions where appropriate. |
| 1.4.11 Non-text Contrast (Level AA) | Supports | Representative controls, control boundaries, focus indicators, and status information were manually reviewed in Windows contrast mode and remained visible and understandable. |
| 1.4.12 Text Spacing (Level AA) | Supports | Representative customer and administration pages were tested with increased line height, letter spacing, word spacing, and paragraph spacing without clipping, overlap, or loss of functionality. |
| 1.4.13 Content on Hover or Focus (Level AA) | Partially Supports | Keyboard focus and representative interactive controls were manually tested without identified blocking issues. A dedicated review of all hover- or focus-triggered supplemental content for dismissibility, hoverability, and persistence has not yet been completed. |
| 2.1.1 Keyboard (Level A) | Supports | Representative customer and administration workflows were traversed using keyboard navigation. Tested interactive controls were reachable and operable without requiring a pointing device. |
| 2.1.2 No Keyboard Trap (Level A) | Supports | Manual keyboard testing did not identify any location where keyboard focus became trapped. Users could continue forward or backward through the tested customer and administration workflows. |
| 2.1.4 Character Key Shortcuts (Level A) | Not Applicable | The evaluated React applications do not provide single-character keyboard shortcuts that trigger application commands. |
| 2.2.1 Timing Adjustable (Level A) | Partially Supports | No task-specific time limits were identified in the evaluated application workflows. Authentication/session-expiration behavior has not yet been evaluated specifically against this criterion, so a full Supports determination is deferred. |
| 2.2.2 Pause, Stop, Hide (Level A) | Supports | The Live Today interface provides a keyboard-accessible "Pause automatic refresh" control for automatically updating information. No other moving, blinking, scrolling, or automatically updating content requiring a pause/stop/hide mechanism was identified in the evaluated interfaces. |
| 2.3.1 Three Flashes or Below Threshold (Level A) | Supports | No flashing content or visual effects approaching the three-flashes threshold were identified in the evaluated React applications. |
| 2.4.1 Bypass Blocks (Level A) | Supports | Both customer and administration applications provide keyboard-accessible skip links that move focus to the main content region. Skip-link behavior is covered by automated tests. |
| 2.4.2 Page Titled (Level A) | Supports | Customer authentication views and administration views provide context-specific document titles. Document-title gaps identified during the B2 audit were remediated and tested. |
| 2.4.3 Focus Order (Level A) | Supports | Keyboard traversal followed a logical interaction sequence in representative customer and administration workflows. Administration route changes were additionally verified with NVDA to move focus to the new page heading appropriately. |
| 2.4.4 Link Purpose (In Context) (Level A) | Partially Supports | Representative navigation, breadcrumb, and action links use descriptive text and automated accessibility checks cover representative states. A dedicated exhaustive review of every link in every application state has not been completed. |
| 2.4.5 Multiple Ways (Level AA) | Supports | Customer navigation provides breadcrumbs, linked overview pages, and local navigation. Administration views below the Libraries root use semantic breadcrumbs, and the production breadcrumb implementation was manually verified. |
| 2.4.6 Headings and Labels (Level AA) | Supports | Representative application headings and labels were reviewed during the B2 audit. Administration heading hierarchy was manually traversed with NVDA and found to provide meaningful page structure. Form and control labels are also covered by automated accessibility tests. |
| 2.4.7 Focus Visible (Level AA) | Supports | Representative customer and administration workflows were manually traversed using the keyboard. Visible focus remained apparent on tested interactive controls, including while Windows contrast mode was enabled. |
| 2.5.1 Pointer Gestures (Level A) | Not Applicable | The evaluated applications do not require multipoint or path-based pointer gestures to operate functionality. |
| 2.5.2 Pointer Cancellation (Level A) | Partially Supports | The evaluated applications primarily use standard browser buttons, links, and form controls rather than custom pointer-down interactions. A dedicated review of every pointer event implementation against this criterion has not been completed. |
| 2.5.3 Label in Name (Level A) | Supports | Tested controls expose accessible names consistent with their visible labels. Representative controls were verified with NVDA, and accessible-name behavior is covered by automated accessibility tests. |
| 2.5.4 Motion Actuation (Level A) | Not Applicable | The evaluated applications do not use device motion or user motion as an input mechanism. |
| 3.1.1 Language of Page (Level A) | Partially Supports | The applications present English-language content. A criterion-specific verification of the programmatic page-language declaration on every production entry document has not yet been recorded. |
| 3.1.2 Language of Parts (Level AA) | Not Applicable | No material passages in another human language requiring a programmatic language change were identified in the evaluated applications. |
| 3.2.1 On Focus (Level A) | Supports | Manual keyboard testing did not identify controls that cause an unexpected change of context merely by receiving focus. |
| 3.2.2 On Input (Level A) | Supports | Customer report range/date controls were manually exercised and changed results without unexpected navigation or unrelated context changes. No known input-triggered context-change exception was identified in the evaluated applications. |
| 3.2.3 Consistent Navigation (Level AA) | Supports | Repeated application navigation is presented in a consistent relative structure within the customer and administration applications. Representative navigation flows were manually reviewed during B2. |
| 3.2.4 Consistent Identification (Level AA) | Supports | Repeated actions and destinations were manually spot-checked in both applications and were consistently identified within each application context. Customer "Sign out" and administration "Log out" are internally consistent within their separate application contexts. |
| 3.3.1 Error Identification (Level A) | Partially Supports | Customer Routing validation was manually verified with NVDA to announce an invalid/error state, and representative form validation is covered by automated tests. Not every validation path in both applications has been manually exercised with assistive technology. |
| 3.3.2 Labels or Instructions (Level A) | Supports | Form-control labeling is covered by automated accessibility checks. B2 remediation added required-field guidance and native required semantics to affected provisioning, account-name, and routing fields. |
| 3.3.3 Error Suggestion (Level AA) | Partially Supports | Tested validation paths provide understandable error information, but a criterion-specific review confirming appropriate correction suggestions for every detectable input error has not yet been completed. |
| 3.3.4 Error Prevention (Legal, Financial, Data) (Level AA) | Partially Supports | SortView includes workflows that modify stored configuration and administrative data. Some consequential operations use explicit actions and application validation, but a criterion-specific review has not yet established that every applicable create, update, suspend, offboard, or other data-changing workflow provides the required reversibility, input checking, or confirmation opportunity. |
| 4.1.1 Parsing (Level A) | Partially Supports | The React applications use standard browser-rendered HTML and representative states pass automated accessibility testing. A complete markup-validation review for duplicate attributes, element nesting, duplicate IDs, and related parsing requirements has not been separately recorded. |
| 4.1.2 Name, Role, Value (Level A) | Supports | Representative customer and administration controls were manually verified with NVDA for meaningful accessible names and roles. Tested chart/table controls, navigation, and application controls exposed understandable information, and representative states are protected by automated accessibility tests. |
| 4.1.3 Status Messages (Level AA) | Partially Supports | Customer Routing validation and successful-save status messages were manually verified with NVDA without requiring focus movement. Automated accessibility tests cover representative status behavior, but every status-message path in the customer and administration applications has not been manually verified with a screen reader. |
## Overall assessment

The evaluated SortView React customer and administration applications demonstrate
substantial support for WCAG 2.1 Level A and Level AA requirements based on the
automated and manual evidence documented above.

Criteria marked **Partially Supports** generally indicate an evidence limitation
rather than a confirmed accessibility defect. These criteria remain conservative
until their complete application surface has been reviewed specifically against
the criterion.

No criterion in this evaluation is currently classified as **Does Not Support**.

This report does not assert blanket WCAG 2.1 Level AA conformance. It records the
level of support that can be defended from the implementation, automated tests,
manual production testing, and assistive-technology testing completed as of
2026-10-19.

## Supporting evidence

Primary verification record:

- `docs/accessibility-verification-2026-10-19.md`

Relevant B2 implementation work includes:

- automated axe-core accessibility testing
- blocking JSX accessibility linting
- skip-link and main-content focus support
- route-arrival focus management
- page-title remediation
- required-field guidance and native required semantics
- semantic administration breadcrumbs
- automated accessibility regression tests
- manual keyboard verification
- manual NVDA verification
- 200% zoom verification
- 320 CSS-pixel reflow verification
- text-spacing verification
- Windows contrast-mode verification

## Remaining accessibility evidence work

The following areas are intentionally retained as **Partially Supports** pending
more exhaustive criterion-specific verification:

- 1.3.1 Info and Relationships
- 1.3.5 Identify Input Purpose
- 1.4.1 Use of Color
- 1.4.13 Content on Hover or Focus
- 2.2.1 Timing Adjustable
- 2.4.4 Link Purpose (In Context)
- 2.5.2 Pointer Cancellation
- 3.1.1 Language of Page
- 3.3.1 Error Identification
- 3.3.3 Error Suggestion
- 4.1.1 Parsing
- 4.1.3 Status Messages

Those classifications should be revisited when additional criterion-specific
testing is performed. A Partially Supports designation here should not be
silently promoted to Supports without additional evidence.
