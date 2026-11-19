# SortView Collector 1.0.16 Release Notes

## Status

- Release: `1.0.16` — built and internally preserved; **not production-qualified**.
- `collector.__version__`: `1.0.16`.
- Source: merged `main`, commit `c0506b0` (PR #318).
- Canonical artifact: `SortViewCollector-1.0.16.zip`, built and verified 2026-11-19.
- Artifact repository: private `richardrhanly-us/sortview-artifacts`.
- Artifact path: `collector/1.0.16/SortViewCollector-1.0.16.zip`.
- Artifact commit: `d252e0d`.
- ZIP size: 35,003,102 bytes.
- ZIP SHA-256: `89b5b7041b14914cddc32a8a34e6b41d373e8614d28dd3d8718e4f9905fcb878`.
- Bundle `MANIFEST.json` SHA-256: `d8255794267a8b5cde3c1ad5aa91df9add98e75647a894001ca6c6a686adaf28`.
- Runtime executable SHA-256: `8a649e1177db24242bd989151b084a24efb3def9b39bef3c6de295bfc76f0e6a`.
- Manifest entries: 1,350.
- Build machine: `LIB-L26`, Python 3.14.3, PyInstaller 6.22.3, hooks-contrib 2026.7.
- Verification: extracted executable reports 1.0.16; all 1,350 manifest entries passed SHA-256 verification; no unlisted files; ZIP security scan returned zero filename and content findings.
- Known warning: PyInstaller reported `Hidden import "jinja2" not found!`; runtime command verification remains pending.
- Production: NBPL remains on Collector 1.0.13. The 1.0.13 → 1.0.16 upgrade and rollback rehearsal has not been performed.
- Approval: internal artifact only; unsigned, unpublished, not customer-approved or production-qualified.

## Why

Hold counts were wrong (`docs/hold-count-accuracy.md`, verified against NBPL production on 2026-11-18):

- **Transits counted as holds.** The SIP2 response `101YNY` ends in an **alert** flag that is raised for transits too.
  Every Collector up to 1.0.15 sent every `101YNY` as `hold`. CV04 transits (in April 2026 about 31% of `101YNY`
  records) were counted as holds, and stored Westside holds and transits cannot be told apart (5,276 such events at
  NBPL since the 2026-09-28 cutover).
- **ILL decided from the wrong patron.** A hold was classified by the item record's patron (`AA`), not the hold patron
  (`CY`). A title containing the word "Ill" also made a hold ILL.

## What changed

Classification only (`collector/v2_classify.py`, `collector/v2_transform.py`, `collector/v2_events.py`). The wire
contract keeps its shape, the identity algorithm and the privacy boundary are unchanged, and nothing new leaves the
machine.

### Hold state from the alert type (CV)

| ACS response | State sent |
|---|---|
| `101YNY` + CV01 (local hold) | `hold` |
| `101YNY` + CV02 (hold for another destination: Westside, Library Express) | `hold` |
| `101YNY` with no CV | `hold` (approved fallback) |
| `101YNY` + CV04 (transit) | `non_hold_101` (an existing state: it retracts an earlier hold of the item) |
| `101YNY` + any other CV (00, 03, 99, unseen) | **`unknown_alert`** (new; neither a hold nor a non-hold) |
| other `101…` | `non_hold_101` |
| other code-10 responses | `other_code10` |

`unknown_alert` has the non-hold shape (`state`, `event_key`, `event_time`, `item_key` only). Like `non_hold_101`, a
later one retracts an earlier hold of the same item from the local hold ledger. The CV code itself is never sent. NBPL's
legacy data (to 2026-09-28) had no CV other than 01, 02 and 04 on a `101YNY`, so `unknown_alert` is expected to be rare.

### The hold patron

- A hold's patron is `CY`, falling back to `AA` only when `CY` is absent. Its cached message-64 profile decides ILL
  (patron type `ILL`, or ILL/INTERLIBRARY in the name) and service accounts, and its late profile corrects the hold.
  The hold ledger is keyed by it.
- Both `AA` and `CY` count as sightings for the local patron cache: their sliding TTL moves, and the patron-card guard
  recognises either as a patron card.
- `CY`, like `AA`, exists on the machine only as a keyed HMAC.

### ILL title rule removed (decision D2)

The raw-message ILL match leaves out the title field (`AJ`). The other indicators are unchanged.

## Compatibility

- **Server first.** The server must accept `unknown_alert`: migration `5813f1df32a0` and the phase B backend
  (`docs/hold-count-accuracy.md`, section 5, B) must be live before 1.0.16 runs anywhere. An older server refuses an
  `unknown_alert` event as an invalid state.
- **Older Collectors are unaffected.** 1.0.13 and 1.0.15 never send `unknown_alert`. The phase B server accepts their
  payloads unchanged; their days are reported as uncorrected (Westside partial).
- **Event identity.** Unchanged algorithm. A record that 1.0.15 sent as `hold` and 1.0.16 re-reads (a rewound cursor)
  is sent with its new state. That makes it a new event at the same `event_time`, stored later, so it wins under the
  server's latest-record rule.
- **Hold ledger from an earlier release.** Rows written by 1.0.15 are keyed by the `AA` patron and stay until their
  normal expiry (90 days). A late profile can still correct them, by the old rule. They belong to days before the
  site's corrected-from date, which the reports already label as uncorrected.
- **Local counters.** The status file and log gain `acs_state_unknown_alert`. Counters stay on the machine.
- **Reports.** A site's v2 holds count as accurate only from its `hold_classification.corrected_from` date, which an
  operator sets with `scripts/set_hold_corrected_from.py` to the **first full local day after the verified upgrade**.
  The upgrade day itself stays uncorrected.

## Before this release is used

1. Phases A and B merged and deployed, and migration `5813f1df32a0` applied to production first. The
   schema-compatibility guard enforces this order.
2. Phase D (the frontend's partial and uncertain labels) deployed with or before phase B.
3. The canonical artifact built from the merged commit and recorded here, by the 1.0.15 procedure.
4. An upgrade and rollback rehearsal from the release NBPL runs.
5. After a verified production upgrade, set the site's `corrected_from`, audited: the first full local day after
   verification, never back-dated (the script refuses today or earlier). **On any rollback below 1.0.16, clear it**
   (`scripts/set_hold_corrected_from.py clear`).

**Rollback order.** (1) Clear the site's `corrected_from`. (2) Roll the Collector back below 1.0.16. (3) Only then roll
the backend below phase B: an older backend refuses the `unknown_alert` events 1.0.16 sends. (4) Only then downgrade
migration `5813f1df32a0`, which refuses while any `unknown_alert` row exists. Rolling back only the frontend is
harmless.

## Tests (local, 2026-11-18, LIB-L26)

Recorded in `docs/PROJECT_MANIFEST.md` (section 5): `tests/test_collector_v2_hold_cv.py` (new),
`tests/test_hold_cv_regression.py`, `tests/test_collector_v2_transform.py` (parity with the corrected legacy classifier,
including CV and `CY` scenarios), `tests/test_collector_v2_privacy.py` (Collector and server states equal again).
