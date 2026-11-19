# Hold-count accuracy: findings and correction plan

**Status (2026-11-18): investigation and production verification complete; the reporting defect is UNRESOLVED.**
Production hold figures (Live Today Holds, Sorter Reports Holds in every range) are still wrong as described in
section 1. Nothing in this document describes a completed fix. The read-only verification Q0–Q5 (section 8) was run by
the product owner; its findings are in section 1.4. No production system, Collector build or database was changed.
**Phases A (legacy classifier), B (v2 partial reporting, the `unknown_alert` state and migration, the corrected-from
marker), C (Collector 1.0.16 source) and D (frontend labels) are implemented locally on branch
`fix/hold-count-accuracy`, not committed, migrated, built or deployed**, so production figures are still wrong and every
sorter still runs its old Collector. The final integration review (2026-11-19) found no correctness defect; the
changes are staged for the user's commit. Next: the release steps (section 5, B and C).

**For:** whoever implements or reviews the correction, and the product owner deciding the open questions (section 9).

## 1. Findings

### 1.1 `101YNY` is not a hold: transits are counted as holds (confirmed defect)

Every hold classifier in SortView decides "hold" from the ACS check-in response prefix `101YNY` alone:

- the legacy classifier, `src/metrics.py::build_acs_item_summary` (`is_hold = raw_message.startswith("101YNY")`);
- Collector Contract v2, `collector/v2_classify.py::item_state`, which sends `state = "hold"` for every `101YNY`;
- and so the server's v2 summary, `src/metrics_v2.py::build_acs_item_summary_v2` (`state == "hold"`).

`101YNY` is the SIP2 check-in response: check-in OK (`1`), resensitize (`Y`), not magnetic media (`N`), **alert** (`Y`).
The last `Y` is an alert, raised for holds **and** for items in transit. The alert type is in the `CV` field.

**Controlled NBPL tests, 2026-11-18** (The High Season: its final scan, 11:12:56 AM, only):

| Condition | Bin | ACS response | CV | Checkins middle flag |
|---|---|---|---|---|
| Normal check-in | 1 | `101YNN` | absent | N |
| Main hold | 0 | `101YNY` | 01 | Y |
| Westside hold | 5 | `101YNY` | 02 | Y |
| Ordinary Westside transit | 5 | `101YNY` | 04 | N |
| Repair exception | 0 | `101YNN` | absent | N |

**April 2026 NBPL logs** (`ACS Log.txt` and `Checkins.txt`, 2026-03-31 to 2026-04-20, about 19,400 scans; analysed
locally, aggregate counts only, no patron data reported). Check-ins matched to their ACS response by item and time
(within 10 s):

| Checkins middle flag | ACS response, CV | Scans | Bin |
|---|---|---|---|
| Y | `101YNY` CV01 | 2,359 | 0 |
| Y | `101YNY` CV02, CT Westside | 401 | 5 |
| Y | `101YNY` CV02, CT Library Express | 277 | 0 |
| Y | `101YNY`, no CV | 2 | 0 |
| N | `101YNY` CV04, CT Westside | 1,367 / 8 | 5 / 0 |
| N | `101YNN` | 14,804 | 0–6 |

- The middle flag (`flag_2` in `agent/parser/checkins.py`) is the sorter's own hold decision. It agreed with CV01/CV02 on
  every matched scan; CV04 never had it.
- Only CV01/CV02 records carry the hold patron (`CY`) and name (`DA`), and the sorter requests a patron record (message
  63 → 64) for exactly those holds.
- Of 4,424 `101YNY` records, 1,382 (31%) were CV04 transits. Counted per item per day, about 1,350 transits were
  counted as holds against about 2,970 real holds: **roughly 46% too many**.
- Not observed in April: CV00, CV03. CV99 appeared only on failed check-ins (`100NNY`, 82).
- Bin 0 is not "Main holds": Library Express holds (CV02) also go to bin 0, and bin 0 also takes exceptions (such as
  In Repair), some transits and "No Agency Destination" items. Holds are never classified by bin.

### 1.2 ILL: the indicator is structured, but the classifiers read the wrong patron

- **The ILL indicator is the hold patron's patron type `ILL`** (`PT` in the message-64 patron record). It is a coded
  value, not free text. 27 of 995 patrons in April had it.
- **It is not** in CT (destination), CV (ILL holds are ordinary CV01 holds), the item's location or media fields, or
  Checkins.txt. CV03 was never observed, and nothing supports reading it as ILL.
- In April, ILL appeared only on holds: 36 item-days, all CV01 Main holds, bin 0. How an item *borrowed from* another
  library looks when it is returned is not known (gap G2).

**The defect.** Both classifiers look up the patron profile of the item record's `AA` field, not of the hold patron
`CY`. Among April's latest hold records per item and day, `AA` differed from `CY` on 724 and was absent on 395. Against
the hold patron's actual type:

| Hold patron's type | Current rule: ILL | Current rule: not ILL |
|---|---|---|
| `ILL` (36) | 34 | 2 (missed) |
| other (2,934) | 1 (false positive) | 2,933 |

The current rule also marks a hold ILL when the **whole raw message** contains the word ILL, which includes the item
title (the documented "ILL-title quirk", `docs/collector-v2.md`). No April title triggered it.

**So `ill_hold_count` is close (35 vs 36 in April) but not exact**, in both directions. No transit was flagged ILL in
April; one could be in future if its previous borrower (`AA`) was an ILL patron.

### 1.3 What each era stored

| Era | Stored | CV / `CY` | Middle flag |
|---|---|---|---|
| Legacy (to the v2 cutover, NBPL 2026-09-28 18:03:41 Chicago) | `acs_events.raw_message` (whole ACS message), `checkins.flag_2` and `bin` | **yes**, in the raw message | **yes** |
| Contract v2 (from the cutover) | `acs_item_events`: `state`, `destination` (from CT), `is_ill`, `is_branch_services`, `is_collection_services` | no | no (the v2 contract forbids it) |

In v2, a CV01 Main hold has no CT, so it arrives with destination `unknown`. A CV02 Library Express hold arrives as
`library_express`. **A CV02 Westside hold and a CV04 Westside transit both arrive as `state = hold`, destination
`westside`**, and their check-ins are identical too (bin 5, `westside`, no flag). The heartbeat's run counters stay on the
machine. **No stored v2 field can separate them, so Westside holds since the cutover cannot be recovered.**

### 1.4 Production verification (NBPL, 2026-11-18)

Run by the product owner with the read-only procedure and SQL of section 8, as reported on 2026-11-18. The result grids
themselves are not reproduced here (aggregates only, and no grid was attached to the report).

**Three counting units appear and must not be mixed:**

- **Records (events):** one row per ACS response or check-in. Repeat scans of one item count several times. Q1, Q2, Q4
  and Q5 count records.
- **Item-days (deduplicated):** each item counted once per local day, by its latest record that day. This is what the
  Holds and ILL figures count (section 2). Q3 counts item-days.
- **Item-months:** each barcode counted once per month, by its latest ACS response in the month. Only the August
  figure below uses it. It is neither a record count nor the sum of item-days (an item held on several days counts
  once), so it is **not** comparable with a monthly Holds KPI total.

| Finding | Unit | Source |
|---|---|---|
| Q0 confirmed NBPL's cutover; Q1–Q5 were run only after it matched (2026-09-28 18:03:41 Chicago) | — | Q0 |
| CV01 = local (Main) holds; CV02 = destination-specific holds (Westside, Library Express); CV04 = non-hold transit — consistent with the April logs and the 2026-11-18 tests | records | Q2 |
| **No CV04 record had Library Express as its destination** | records | Q2 |
| Legacy ACS records keep the fields the correction needs (CV, `CY`, CT in `raw_message`) | records | Q2 |
| **August 2026: 5,627 hold candidates under the old `101YNY` rule: 2,381 CV04 transits and 3,226 CV01/CV02 holds** (about 42% of the candidates were transits; the remaining 20 are the other `101YNY` candidates, presumably missing CV — not itemized in the report) | **item-months** (latest ACS response per barcode within the month), not item-days | additional production query, reported 2026-11-18 |
| Legacy successful alerts (`101YNY`) carried only CV01, CV02 and CV04, plus records with no CV; **no other CV code** | records | Q2 |
| **5,276 stored v2 `hold` events with destination `westside`**, each ambiguous between a Westside hold and a Westside transit | **records (events)** — not items, not item-days | Q4 |
| **345 legacy item-days whose hold patron (`CY`) has patron type `ILL`; 9 of them have no `AA`** (the current rule cannot see those 9 at all) | item-days | Q3 |
| `CY` is the hold patron and `AA` the item-record patron, confirmed at scale | item-days | Q3 |
| Existing v2 rows cannot separate Westside holds from Westside transits | records | Q4, Q5 |

**The August figure is item-months** (confirmed by the product owner, 2026-11-18): the latest ACS response per barcode
within August, from a query outside Q0–Q5. It shows the scale of the transit problem; it is not a monthly Holds total.
The 20 candidates that are neither CV04 nor CV01/CV02 are inferred from the arithmetic, not itemized.

**Consequences:** legacy days are expected to show `unknown_alert_total` 0 (Q2 found no other CV on `101YNY`), so the
D4 path matters for the future (v2/1.0.16), not for history. The v2 Westside ambiguity is large: 5,276 events since
the cutover, so most v2 days with Westside activity will be labelled partial until `corrected_from`.

**Not yet recorded from the results:** Q1's monthly middle-flag coverage figures (not needed: the correction reads CV
from the ACS records, not the middle flag).

**ILL (Q3) is a coverage comparison, not an exact reconciliation:** it takes each patron's latest type over the whole
legacy era, where the report uses a 180-day lookback.

## 2. Approved reporting policy (product owner, 2026-11-18)

- **Holds KPI:** confirmed ordinary patron holds, **including staff/employee holds**. Excludes ILL holds, configured
  service-account holds, transits, rejected items and exceptions.
- **ILL KPI:** confirmed ILL holds, counted separately and never in Holds.
- **Service-account holds:** kept as their own classification, even without a KPI.
- **History:** legacy classifications are corrected from stored evidence. Where v2 records cannot separate Westside
  holds from transits, show an **explicitly labelled partial count** that explains the unavailable Westside part.
  **Never an estimate presented as accurate, and never zero for unavailable records.**
- **Repeat scans:** the latest relevant record per item per local day (the rule already in use since UI2 v7).

## 3. Classification rules

Applied to each item's **latest `101…` record of the local day** (legacy: by barcode; v2: by `item_key`, greatest
`(event_time, id)`), exactly as today. `other_code10` records are ignored, as today.

**Item state**

| Record | State | Counted as |
|---|---|---|
| `101YNY` + CV01 | hold | a hold (Main: no CT) |
| `101YNY` + CV02 | hold | a hold (destination from CT: Westside, Library Express) |
| `101YNY`, no CV | hold | a hold (2 in April; the sorter flagged both) |
| `101YNY` + CV04 | not a hold (transit) | nothing |
| `101YNY` + any other CV (00, 03, 99, unseen) | **unknown alert** (D4: not a confirmed hold, not a confirmed non-hold) | not in Holds or ILL; counted as **uncertain** and reported (section 5.1) |
| other `101…` (`101YNN`: normal or exception) | not a hold | nothing |
| `100…` (failed check-in) and other code 10 | other | nothing (rejects are reported from the Rejects file) |

**A hold's category** (one per hold; precedence as today):

1. **ILL** if the hold patron is ILL: the hold patron is `CY`, falling back to `AA` only when `CY` is absent. ILL if that
   patron's type is `ILL` or its name contains ILL/INTERLIBRARY, or the destination or the raw message **excluding the
   title field (`AJ`)** says so.
2. **Service account** if the hold patron's name is a configured Branch Services / Collection Services account, or the
   message carries a configured `|pattern|` marker.
3. **Public (Holds KPI)** otherwise, including staff and employee patrons.

`holds_total` (public), `ill_total`, and the service-account totals stay the classifier's existing outputs.

## 4. Data availability

| Days | Holds KPI | ILL KPI |
|---|---|---|
| Legacy | **accurate** after correction A | **accurate** after correction A |
| v2, before the branch's corrected-from date | **partial** on any day with an ambiguous hold row: Main and Library Express (D1, validated by Q2: no CV04 went to Library Express); the Westside part is unavailable | as sent, with an **accuracy note** (D3: the `AA`/`CY` problem; 34/36 + 1 in April terms) |
| v2, from the corrected-from date | **accurate** | **accurate** |

In every era, an item-day whose latest record is an **unknown alert** is counted in neither KPI and reported as
uncertain (D4).

A v2 day **without** any ambiguous hold row is complete: if no item ended the day as a `westside` hold, there was neither
a Westside hold nor a counted transit.

## 5. Implementation plan (approved order A → B → C → D; all four implemented locally, under review)

### 5.1 Confirmed, unavailable and uncertain (requirement, 2026-11-18)

The report keeps its two confirmed counts and adds **three integers**, each needed to say something the others cannot:

| Field | Meaning | Shown as |
|---|---|---|
| `public_hold_count`, `ill_hold_count` (existing) | **confirmed** holds only, by the policy in section 2 | the KPI figures |
| `public_holds_partial_days` (new) | days whose Holds figure lacks an **unavailable** part: v2 days before `corrected_from` with at least one ambiguous (`westside`) hold | "Partial" label + the Westside explanation |
| `ill_uncertain_days` (new) | v2 days before `corrected_from` with at least one hold, whose ILL figure may be off by the `AA`/`CY` problem (D3) | a scoped note on the ILL figure only |
| `unknown_alert_count` (new) | item-days whose latest record is an **unknown alert type**, counted in neither KPI | "N items with an unrecognised alert type were not counted" |

None of these is ever folded into a KPI, and none is shown as a zero hold count.

### 5.2 Unknown alert types with the existing v2 states (D4)

The existing states cannot carry "unknown alert" to the server without losing its meaning:

- `non_hold_101` would assert a confirmed non-hold (and retract an earlier hold of the item) — D4 rules this out.
- `other_code10` also covers failed check-ins (`100…`), and a strictly later one does not retract an earlier hold, so
  the server could neither tell it apart nor know the item-day is uncertain.
- `hold` with a sentinel destination would be counted as a hold.
- The Collector's quarantine is for events the **server** refuses; reusing it would report `permanent_rejection`,
  mark the Collector degraded and trip the pipeline-health alert, and still say nothing per day.

**Legacy era:** no problem — the backend reads CV from the stored raw message and counts unknown alerts directly.

**Contract v2, recommended (needs approval: a contract change):** one new state value, `unknown_alert`, with the
non-hold shape (`event_key`, `event_time`, `item_key`, `state` only — no patron or raw field). It needs the server enum
(`src/services/ingest_v2_models.py`), the `acs_item_events_state_chk` CHECK constraint (a migration) and the server
summary, **deployed before** any 1.0.16 Collector sends it; the CV code itself is not sent. The latest-record rule then
makes the item-day uncertain, and it is counted in `unknown_alert_count`.

**Fallback if no contract change is allowed:** 1.0.16 sends unknown alerts as `other_code10` and counts them locally
(`collector.log` and the local status document). Server reports then cannot flag them; this fails requirement 1 for v2
and is not recommended. No unknown alert was seen on a `101YNY` in April; Q2 shows how often they occurred in the
whole legacy era: none (section 1.4).

### A. Legacy historical correction (backend only) — implemented locally 2026-11-18, under review

**As implemented:** `metrics.hold_patron_id(raw_message, patron_id)` (CY, else AA) and `_alert_kind` (section 3);
`build_acs_item_summary` returns `unknown_alert_total` (not yet exposed by the API: phase B); the report service's
lookback names patrons with `hold_patron_id`. API responses are unchanged (three integers). **One deviation from the
plan below:** the Collector source was not changed with A (instruction for this phase: no Collector change yet), so the
title rule now differs between the legacy and v2 classifiers until phase C. The parity scenario "TITLE quirk" in
`tests/test_collector_v2_transform.py` is a strict expected failure until then; the Collector's own
`test_the_ill_title_quirk_is_reproduced_on_purpose` still passes, and is rewritten in C. It affects no report:
legacy days use only the legacy classifier and v2 days only the Collector's flags.


- `src/metrics.py::build_acs_item_summary`
  - `is_hold`: the CV rule of section 3 (parse `|CV(\d\d)` from `raw_message`); a new `unknown_alert_total` output
    for item-days whose latest record is an unknown alert.
  - The hold patron: `CY` from `raw_message`, else `patron_id` (`AA`); patron profiles are merged on it.
  - The ILL raw-message match excludes the `AJ` (title) field (decision D2).
- `src/services/hold_report_service.py::_count_days`: the patrons a legacy day names for its lookback are the items'
  hold patrons (`CY`, else `AA`), not `patron_id` alone. The SQL is unchanged (it already reads `raw_message`).
- Parity: `tests/test_collector_v2_transform.py` and `tests/test_collector_v2_late_profile.py` compare the Collector's
  classifier with this one. Their fixtures have no CV or `CY`, and the new rule's fallbacks (no CV → hold, no `CY` →
  `AA`) leave those results unchanged; CV/`CY` fixtures are added with change C so both sides move together.
  **Except the title quirk:** `test_the_ill_title_quirk_is_reproduced_on_purpose` (Collector) and the "title" case in
  `tests/test_v1_hold_classification.py` pin it on purpose. If D2 removes it, A's `metrics.py` change and C's
  `v2_classify.py` source change land in the same change set (the 1.0.16 *release* still comes later), and both tests
  are rewritten to assert the title no longer counts.

### B. Contract v2 correction and partial labelling (backend only) — implemented locally 2026-11-18, under review

**As implemented:**

- `src/metrics_v2.py::build_acs_item_summary_v2(df, labels, ambiguous_destinations=frozenset())`: `unknown_alert` rows
  take part in the latest-record rule and are counted in `unknown_alert_total`; a public hold at an ambiguous
  destination is left out of `holds_total` and counted in `ambiguous_hold_total`; `hold_items_total` is every hold.
  The default (no ambiguous set) is the old behaviour.
- `src/services/hold_report_service.py`: reads the branch marker (`_HOLD_CLASSIFICATION`, branch document only, only
  when the v2 era owns part of the range); a v2 day is corrected iff its local date ≥ `corrected_from`; uncorrected
  days use `AMBIGUOUS_V2_DESTINATIONS = {"westside"}` (D1). Per day: partial iff an ambiguous public hold was left out;
  ILL-uncertain iff uncorrected and any hold; unknown alerts in both eras. `HoldsReport` gains the three integers
  (defaults 0).
- `src/customer_api/holds_report_schemas.py` / `holds_report_routes.py`: the three fields; the original four are
  unchanged. The frontend parser keeps only the fields it knows, so the deployed frontend is unaffected (but see the
  deployment prerequisites below).
- `src/services/hold_classification.py`: the marker's key, parser (anything but exactly `{"corrected_from":
  "YYYY-MM-DD"}` is no marker) and stored form.
- `scripts/set_hold_corrected_from.py`: `review` / `set` / `clear`, dry run by default, `--confirm-branch-name`,
  required `--operator-email`, optional `--note`; one transaction with an `auth_audit_log` row
  (`hold_classification_corrected_from_set` / `_cleared`: branch, previous, new, operator, note); only the
  `hold_classification` key changes, verified before commit.
- `unknown_alert`: `src/services/ingest_v2_models.py` (`ACS_ITEM_STATES`, the non-hold shape) and migration
  `alembic/versions/5813f1df32a0_add_acs_item_unknown_alert_state.py` (widens `acs_item_events_state_chk` and
  `acs_item_events_state_shape_chk`; 5-second lock timeout; downgrade refuses while an `unknown_alert` row exists).

**D5 back-dating (decided by the product owner, 2026-11-19): `corrected_from` must not be back-dated.** The script
refuses a `corrected_from` that is today or earlier in the product's zone, and keeps doing so. "The first full local day after verification" is always a future day when the marker is set right
after verification; refusing past dates means a marker can never reach back over a day that had already begun. The
cost: if the operator sets it late, a few corrected days stay labelled partial (the conservative side), which is
accepted.

**Deployment prerequisites (none done):** (1) migration `5813f1df32a0` to production before this code (the
schema-compatibility guard refuses the reverse), and before any 1.0.16 collector; (2) phase D's frontend labels
deployed **with or before** this backend: from the moment it is live, Holds figures for uncorrected v2 days drop by the
Westside part, and the current frontend would show that smaller number without the "Partial" explanation; (3) phase A
changes legacy figures at the same deploy (they become accurate, and smaller).


- `src/metrics_v2.py::build_acs_item_summary_v2`: an optional set of **ambiguous destinations**, applied after the
  latest-record-per-item step: a public hold whose destination is in the set is not counted, and the summary reports
  whether any was found. ILL and service-account holds are counted as now. An `unknown_alert` latest record (once the
  state exists) counts in `unknown_alert_total`, in neither KPI.
- `src/services/hold_report_service.py`: for v2 days before the branch's corrected-from date, pass the ambiguous set
  (`{"westside"}` only: D1 is settled, Library Express holds count as accurate); count the days where an ambiguous
  hold was found;
  return them as `public_holds_partial_days`. A cutover day's v2 part counts.
- `src/customer_api/holds_report_schemas.py` and `holds_report_routes.py`: the three fields of section 5.1.
  `public_hold_count` and `ill_hold_count` become "confirmed"; the existing `days_with_data` is unchanged.
- **Corrected-from date, per branch (D5, provisionally approved):** a `hold_classification.corrected_from` local date
  in the branch's settings document (`branch_settings.settings_json`), read with the hold rules the report already
  reads. **No schema change.** Absent means every v2 day is uncorrected. It is set per branch, so branches upgrade
  independently. Conditions:
  - **It is the first full local day of verified corrected collection** (section 6), never the upgrade day.
  - **Audited:** every set or clear writes an `auth_audit_log` row (the table the admin services already audit to,
    through `auth_service.log_auth_event_with_connection`) in the same transaction, with the branch, the previous and
    new values and the operator. The existing bin-layout script writes no audit row; this one must.
  - **Rollback clears it.** A branch taken below 1.0.16 has the marker cleared (audited), and every v2 day is
    uncorrected again — conservative: a corrected period is then labelled partial, never the reverse. After a new
    verified upgrade it is set again to the new first full day.
  - **Never customer-editable:** the customer settings routes change only their own keys; a test proves they leave
    `hold_classification` untouched.

### C. Collector 1.0.16 (after 1.0.15; the frozen 1.0.15 artifact is untouched) — implemented locally 2026-11-18, under review

**As implemented:** `collector/v2_classify.py` (`item_state` by CV; `hold_patron(raw, aa)`; `static_flags` without
`AJ`); `collector/v2_events.py` (`unknown_alert` in the state tuples, the non-hold shape); `collector/v2_transform.py`
(the hold patron keys the profile lookup and the hold ledger; both `AA` and `CY` are cache sightings, so the
patron-card guard knows both; a later `unknown_alert`, like `non_hold_101`, removes the item's ledger row);
`collector/__init__.py` `__version__ = "1.0.16"`; `docs/releases/collector-1.0.16.md`. The local status counter
`acs_state_unknown_alert` is the "local counter" of the plan; no separate counter was added. Differences from the
plan text: none in behaviour. Two choices the plan did not spell out are recorded in the release note: `CY` sightings
also feed the patron-card guard (privacy-protective), and ledger rows written by 1.0.15 (keyed by `AA`) are left to
expire normally rather than purged (they concern uncorrected days only). No bundle or artifact was built.


1.0.15 was built from `2565b95` and is stored; these source changes are for 1.0.16 only.

- `collector/v2_classify.py::item_state`: the CV rule of section 3. CV04 becomes `non_hold_101`, an existing state that
  already retracts an earlier hold of the same item. Unknown alert types become `unknown_alert` (section 5.2; needs the
  approved contract addition, server first).
- `collector/v2_classify.py`: a `hold_patron(raw)` helper (`CY`); `static_flags` drops the `AJ` field from its raw-message
  ILL match (D2).
- `collector/v2_transform.py::_transform_acs`: the patron key of a hold is the hold patron (`CY`, else `AA`), for the
  profile lookup and the hold ledger alike; a local `acs_unsupported_alert` counter.
- **No other change** to the payload, the API, the schema or the server; the only contract change is the
  `unknown_alert` state value, if approved. `CY`, like `AA` today, is only ever HMAC-keyed on the machine; nothing new
  leaves it. The privacy and prohibited-field tests must pass unchanged.
- Version 1.0.16, `docs/releases/collector-1.0.16.md`, and the hold definition in `docs/collector-v2.md` and
  `docs/contract-v2-design.md` updated with the code.
- Activation: after a branch's upgrade is verified (installation Active, reporting 1.0.16, a scheduled run with
  result 0), an operator sets that branch's `corrected_from` to the **first full local day after verification**, with a
  small operator script modelled on `scripts/set_sorter_bin_layout.py` (review / dry run by default / single-key change
  in one transaction / audited / never from a city machine). A rollback below 1.0.16 clears it (section 5, B).

### D. Frontend — implemented locally 2026-11-18, under review

**As implemented:**

- `frontend/src/api/reports.ts`: `HoldsReport` gains `public_holds_partial_days`, `ill_uncertain_days` and
  `unknown_alert_count`, each a nonnegative integer (`count`, as the other counts). A field that is **absent** reads as
  0: a backend older than B sends none of them and has no partial day, which is what lets D be deployed with or before
  B. A field that is present and invalid, or partial / ILL-uncertain days above `days_with_data`, refuses the answer.
- `frontend/src/reports/HoldsSection.tsx`: the Public holds card's note is **Partial** when any day is; the ILL card's
  note is **May miss or add a few** when any day is ILL-uncertain; one `role="note"` box under the figures explains each
  that applies ("On N of the D days in this date range …", "On this day …" for one day), and unknown alerts ("N items
  had an alert type that was not recognized, so they are not included in either figure."). `HOLDS_EXPLANATION` now
  says items in transit are not holds and only confirmed holds are counted.
- `frontend/src/liveToday/LiveToday.tsx`: the same labels on today's Holds and ILL cards, and one-sentence notes under
  them, only once the holds read is ready (loading and error states unchanged, no label).
- **Deviation from the text above (approved by the product owner, 2026-11-18):** the wording does **not name
  Westside**. The frontend serves every organization, and the existing guard `frontend/src/test/scope.test.ts`
  ("names no destination and no library of its own: routing comes from the API") forbids a hard-coded branch name.
  The notes say "holds for pickup at another location could not be told apart from items in transit to it". If the
  branch should be named, the API would have to send its label (a backend change).
- Days and boundaries (the cutover, `corrected_from`) are the server's (B); the frontend shows the counts it is given.


- `frontend/src/api/reports.ts`: `HoldsReport.public_holds_partial_days`, validated like the other counts.
- `frontend/src/liveToday/LiveToday.tsx`: the Holds card keeps its number and, when the day is partial, a note:
  "Partial: Westside holds are not available from this sorter's current collector."
- `frontend/src/reports/HoldsSection.tsx`: the Public holds card says "Partial" when any day is partial, with an
  explanation naming the partial days ("On N of the D days, holds for pickup at Westside could not be told apart from
  items in transit, so they are not included."). `HOLDS_EXPLANATION` also says transits are not holds.
- ILL (D3): when `ill_uncertain_days` > 0, a note on the ILL figure only: "Before this sorter's collector was updated,
  a few interlibrary loan holds could be missed or added." Unknown alerts: when `unknown_alert_count` > 0, a note
  under both figures. A partial count is never shown as zero for the missing part, and an unavailable or uncertain
  count is never shown as `0`.

## 6. Ranges across the cutover and the corrected-from date

- A range's totals stay the **sum of its days**; a one-day range still equals Live Today.
- Legacy days: accurate. v2 days before `corrected_from`: partial where ambiguous. v2 days from `corrected_from`:
  accurate. The cutover day: its legacy part accurate, its v2 part as an uncorrected v2 day.
- `public_holds_partial_days` and `ill_uncertain_days` count only the range's affected days, so a range entirely before
  the cutover or from `corrected_from` reports 0 for both and shows no label.

**A day with both old and corrected Collector data** (the upgrade day, or a day of a rollback). v2 events carry no
Collector version, so such a day cannot be split. It is treated **entirely as uncorrected**: that is why
`corrected_from` is the first *full* day of verified corrected collection. On that day:

- a transit already sent by 1.0.16 as `non_hold_101` is simply not a hold — correct under either treatment;
- a Westside hold sent by either version is excluded and the day is labelled partial — conservative;
- ILL carries the D3 note; an `unknown_alert` from 1.0.16 is still counted as uncertain.

So a mixed day can be labelled less complete than it is, never more. The same holds for a day after a rollback, once
the marker is cleared.

## 7. Regression tests

**Added 2026-11-18: `tests/test_hold_cv_regression.py`** (synthetic data, shaped like the 2026-11-18 tests). Tests of
behaviour that is already right and must stay right (CV01 and CV02 holds, `101YNY` without CV, `101YNN`, a failed
`100NNY` CV99, transit-then-hold, the `AA` fallback, a staff hold, and the Collector states that do not change) pass now.
Tests of the corrected behaviour are **strict expected failures** (`xfail(strict=True)`), one marker per phase:

- phase A (**markers removed 2026-11-18; these now pass**): CV04 is not a hold; an unrecognised CV is in neither KPI
  and is counted in `unknown_alert_total`; hold-then-transit counts 0; the 2026-11-18 day counts 2; ILL by `CY` (an ILL
  hold patron with no `AA`; an ILL `AA` with an ordinary `CY`); a title with "Ill" is not ILL. Also: the "title" case
  of `tests/test_v1_hold_classification.py` now asserts the title is ignored (and a `|DA` hold-patron-name case
  replaces it in the ILL-indicator test), and `tests/test_hold_report_service.py` proves the lookback follows `CY`
  (it fails against the old `AA` lookback);
- phase C: `item_state` gives `non_hold_101` for CV04; `static_flags` ignores the title;
- D4: `item_state` gives `unknown_alert` for an unrecognised CV.

Phase B tests (added 2026-11-18, all passing): `tests/test_hold_report_service.py` (Westside left out and partial;
Main and Library Express confirmed; ILL/service holds at Westside unaffected; the 2026-11-18 day before and after
correction; a range across the cutover and `corrected_from`; absent/malformed/text markers; the upgrade day; a cleared
marker; `unknown_alert` latest-record and ties); `tests/test_customer_api_holds_report.py` (the seven fields, distinct
values, nothing private); `tests/test_hold_classification_marker.py` (parser; no `src/` write can touch the marker);
`tests/test_acs_unknown_alert_migration.py` (offline SQL); PostgreSQL: `tests/test_hold_corrected_from_postgres.py`
(set/clear/re-set audited, refusals unaudited, other keys untouched, per branch, organization settings ignored),
`tests/test_hold_report_postgres.py` (marker read by the RLS-bound runtime role, cross-tenant, `unknown_alert`),
`tests/test_ingest_v2_postgres.py` (the state and shape constraints; downgrade refusal and round trip). Until phase
C, `tests/test_collector_v2_privacy.py` allows the server exactly one state the collector lacks (`unknown_alert`), with
a strict xfail for equality.

When a phase lands, its tests pass, strict xfail turns that into a failure, and the marker is removed in the same
change. The rest of the list below is still to be written with its phase.

**Legacy classifier (`tests/test_v1_hold_classification.py`, `tests/test_hold_report_service.py`, Postgres:
`tests/test_hold_report_postgres.py`)**, with records shaped like the 2026-11-18 tests:

- Wreckage (`101YNN`, no CV) → not a hold. A Secret Affair (`101YNY` CV01) → public hold. The High Season (`101YNY`
  CV02, CT Westside, final scan) → public hold, its earlier scans ignored. Tom and Jerry (`101YNY` CV04) → nothing.
  When I'm Gone (`101YNN`, repair) → nothing. **Day total: Holds 2, ILL 0.**
- `101YNY` with no CV → hold; with CV00 / CV03 / CV99 → in neither KPI and counted in `unknown_alert_count`;
  `100NNY` CV99 → not a hold and not an unknown alert (a failed check-in).
- ILL by `CY`: hold patron `ILL` with `AA` absent → ILL; `AA` an ILL previous borrower but `CY` not → public; `CY`
  absent → `AA` fallback. A title containing "Ill" → not ILL (D2).
- Staff (`EMPLOYEES`) hold → public. Service-account hold → neither public nor ILL; ILL wins over service.
- Same day: hold → transit counts 0; transit → hold counts 1; repeated hold scans count 1.
- The legacy patron lookback follows `CY`.

**v2 (`tests/test_metrics_v2_parity.py`, `tests/test_hold_report_service.py`, `tests/test_customer_api_holds_report.py`):**

- Before `corrected_from`: `unknown` and `library_express` holds counted (D1, settled), `westside` holds not; the day is
  partial; ILL and service holds unaffected. A day with no ambiguous row is not partial.
- The 2026-11-18 day as v2 data today (A Secret Affair `unknown`; The High Season and Tom and Jerry `westside`):
  Holds 1, partial. From `corrected_from` (1.0.16 sends Tom and Jerry as `non_hold_101`): Holds 2, not partial.
- A range across the cutover and across `corrected_from` sums its days and reports the right partial and uncertain
  day counts; a missing settings key means uncorrected; the upgrade day (mixed data) is uncorrected; a cleared marker
  makes every v2 day uncorrected again; `ill_uncertain_days` only on uncorrected v2 days with a hold; an
  `unknown_alert` row (if approved) is latest-record-wins and counted only in `unknown_alert_count`.
- Setting or clearing `corrected_from` writes an audit row; the customer settings routes leave it untouched.

**Collector 1.0.16 (`tests/test_collector_v2_transform.py`, `tests/test_collector_v2_late_profile.py`):**

- `item_state` for every CV case; the hold ledger and corrections keyed by the `CY` patron; parity with the corrected
  legacy classifier on CV/`CY` fixtures; payload shape unchanged and the privacy/prohibited-field tests unchanged;
  the 1.0.15 artifact's recorded hashes unchanged.

**Frontend (Vitest):** `frontend/src/api/holds.test.ts` (the new field parsed and validated),
`frontend/src/reports/HoldsSection.test.tsx`, `frontend/src/reports/longRange.test.tsx`,
`frontend/src/liveToday/LiveToday.test.tsx`: partial note shown only when partial; never `0` for unavailable; the
accessibility and axe suites; `frontend/src/test/http.ts` fixtures gain the field.

## 8. Production verification (completed 2026-11-18; read-only)

**Completed.** The exact SQL below was approved and run on 2026-11-18 by the product owner in the Neon SQL Editor
(Q0 first, then Q1–Q5 unchanged). Findings: section 1.4. Kept here as the record of what was run; it does not need to
be run again.

- **Target:** NBPL production (Neon): operational customer 1, branch 1 — NBPL's own rows only, by the
  `customer_id = 1 AND branch_id = 1` filter in every query.
- **Procedure:** as on 2026-11-16, in the Neon SQL Editor, the whole script in one read-only transaction that is rolled
  back: `BEGIN TRANSACTION READ ONLY;` then `SET LOCAL statement_timeout = '120s';`, the queries, `ROLLBACK;`. A
  read-only transaction refuses any write, so nothing can change even by mistake.
- **Scope:** `SELECT` only. Tables read: `v2_cutovers`, `checkins`, `acs_events`, `acs_item_events`,
  `checkin_events`. No function with side effects; no temporary table.
- **Output:** counts, months and category codes only. No barcode, patron id, name, title or raw message is output;
  patron ids are matched inside Q3 only.
- **Validated offline (2026-11-18)** on the local disposable PostgreSQL, against temporary tables with the migrations'
  columns and synthetic rows shaped like the 2026-11-18 tests; each query returned the expected rows, and the
  transaction was rolled back.
- **Stop conditions:** if Q0 is not 2026-09-28 18:03:41 Chicago, the date bounds in Q1–Q5 are wrong: stop and revise.
  If Q4/Q5 return no rows, row-level security is filtering the role: stop, do not change roles.

| Query | Reads | Joins | Date bounds | Output columns |
|---|---|---|---|---|
| Q0 | `v2_cutovers` | none | none (all rows of the branch) | `cutover_at`, `set_at` |
| Q1 | `checkins` | none | `event_time < 2026-09-28 18:03:41` (legacy naive local time) | `month`, `checkins`, `middle_flag_y`, `middle_flag_n`, `middle_flag_missing`, `flag_y_bin_0`, `flag_y_bin_5` |
| Q2 | `acs_events` (code-10 responses) | none | as Q1 | `month`, `response` (first 6 characters, e.g. `101YNY`), `cv`, `ct_destination`, `records`, `with_cy`, `with_aa` |
| Q3 | `acs_events` (101 records; patron records) | `item_days` LEFT JOIN `patron_types` twice: on the hold patron (`CY`) and on the item-record patron (`AA`), both by patron id, within NBPL's rows | items as Q1; patron records: all of NBPL's legacy rows (latest type per patron) | `cv`, `hold_patron_cy`, `item_patron_aa` (each `ILL` / `other` / `no patron record` / `no CY` or `no AA`), `item_days` |
| Q4 | `acs_item_events` | none | `event_time >= 2026-09-28 18:03:41 America/Chicago` to now | `month`, `state`, `destination`, `records`, `ill`, `service_account` |
| Q5 | `checkin_events` (bins 0, 5) | none | as Q4 | `month`, `destination`, `bin`, `checkins` |

Notes: Q3 takes each item's latest `101` record per day (by `event_time`, then `id`), as the reports do, and each
patron's latest type over the whole legacy era (an approximation of the report's 180-day lookback, adequate for
coverage). Q4 counts records, not item-days (a correction is a second record). Q1 and Q2 have no lower bound: the legacy
era begins at NBPL's first record (2026-03-31).

```sql
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '120s';

-- Q0. NBPL's v2 cutover boundary (expected: 2026-09-28 18:03:41 America/Chicago).
SELECT cutover_at, set_at
FROM v2_cutovers
WHERE customer_id = 1 AND branch_id = 1
ORDER BY set_at DESC;

-- Q1. Legacy Checkins: middle-flag (flag_2) coverage by month, and flagged holds in bins 0 and 5.
SELECT date_trunc('month', event_time)::date AS month,
       count(*) AS checkins,
       count(*) FILTER (WHERE btrim(flag_2) = 'Y') AS middle_flag_y,
       count(*) FILTER (WHERE btrim(flag_2) = 'N') AS middle_flag_n,
       count(*) FILTER (WHERE flag_2 IS NULL OR btrim(flag_2) NOT IN ('Y', 'N')) AS middle_flag_missing,
       count(*) FILTER (WHERE btrim(flag_2) = 'Y' AND btrim(bin) = '0') AS flag_y_bin_0,
       count(*) FILTER (WHERE btrim(flag_2) = 'Y' AND btrim(bin) = '5') AS flag_y_bin_5
FROM checkins
WHERE customer_id = 1 AND branch_id = 1
  AND event_time < TIMESTAMP '2026-09-28 18:03:41'
GROUP BY 1
ORDER BY 1;

-- Q2. Legacy ACS check-in responses: response prefix x CV code by month, with CY / AA presence. Counts only.
SELECT date_trunc('month', event_time)::date AS month,
       left(raw_message, 6) AS response,
       COALESCE(substring(raw_message FROM '\|CV([0-9]{2})'), 'none') AS cv,
       COALESCE(NULLIF(btrim(destination), ''), 'none') AS ct_destination,
       count(*) AS records,
       count(*) FILTER (WHERE raw_message ~ '\|CY[^|]') AS with_cy,
       count(*) FILTER (WHERE NULLIF(btrim(patron_id), '') IS NOT NULL) AS with_aa
FROM acs_events
WHERE customer_id = 1 AND branch_id = 1
  AND event_time < TIMESTAMP '2026-09-28 18:03:41'
  AND raw_message LIKE '10%'
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3, 4;

-- Q3. Legacy ILL: latest 101 record per item per day, hold patron (CY) type vs item-record patron (AA) type.
--     Patron ids are joined inside the query only; the output is counts.
WITH item_days AS (
    SELECT DISTINCT ON (barcode, event_time::date)
           raw_message,
           NULLIF(btrim(patron_id), '') AS aa,
           NULLIF(btrim(substring(raw_message FROM '\|CY([^|]*)')), '') AS cy
    FROM acs_events
    WHERE customer_id = 1 AND branch_id = 1
      AND event_time < TIMESTAMP '2026-09-28 18:03:41'
      AND raw_message LIKE '101%'
      AND barcode IS NOT NULL
    ORDER BY barcode, event_time::date, event_time DESC, id DESC
), patron_types AS (
    SELECT DISTINCT ON (btrim(patron_id))
           btrim(patron_id) AS patron_id,
           upper(btrim(substring(raw_message FROM '\|PT([^|]*)'))) AS patron_type
    FROM acs_events
    WHERE customer_id = 1 AND branch_id = 1
      AND btrim(message_code) = '64'
      AND NULLIF(btrim(patron_id), '') IS NOT NULL
    ORDER BY btrim(patron_id), event_time DESC, id DESC
)
SELECT COALESCE(substring(d.raw_message FROM '\|CV([0-9]{2})'), 'none') AS cv,
       CASE WHEN d.cy IS NULL THEN 'no CY' WHEN cy.patron_id IS NULL THEN 'no patron record'
            WHEN cy.patron_type = 'ILL' THEN 'ILL' ELSE 'other' END AS hold_patron_cy,
       CASE WHEN d.aa IS NULL THEN 'no AA' WHEN aa.patron_id IS NULL THEN 'no patron record'
            WHEN aa.patron_type = 'ILL' THEN 'ILL' ELSE 'other' END AS item_patron_aa,
       count(*) AS item_days
FROM item_days d
LEFT JOIN patron_types cy ON cy.patron_id = d.cy
LEFT JOIN patron_types aa ON aa.patron_id = d.aa
WHERE d.raw_message LIKE '101YNY%'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- Q4. Contract v2 since the cutover: ACS item records by month, state and destination, with flags.
SELECT date_trunc('month', event_time AT TIME ZONE 'America/Chicago')::date AS month,
       state,
       destination,
       count(*) AS records,
       count(*) FILTER (WHERE is_ill) AS ill,
       count(*) FILTER (WHERE is_branch_services OR is_collection_services) AS service_account
FROM acs_item_events
WHERE customer_id = 1 AND branch_id = 1
  AND event_time >= TIMESTAMPTZ '2026-09-28 18:03:41 America/Chicago'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

-- Q5. Contract v2 check-ins to bins 0 and 5 by destination, for comparison with Q4.
SELECT date_trunc('month', event_time AT TIME ZONE 'America/Chicago')::date AS month,
       destination,
       bin,
       count(*) AS checkins
FROM checkin_events
WHERE customer_id = 1 AND branch_id = 1
  AND event_time >= TIMESTAMPTZ '2026-09-28 18:03:41 America/Chicago'
  AND bin IN ('0', '5')
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

ROLLBACK;
```

**What they settle:** Q1/Q2 that the legacy evidence covers the whole legacy era (correction A's reach) and how many
unknown alert types occurred (D4); Q2 whether any CV04 transit ever went to Library Express (D1); Q3 the size of the
ILL change; Q4/Q5 the size of the v2 Westside ambiguity.

## 9. Decisions and evidence gaps

**Decisions (product owner, 2026-11-18)**

- **D1. Settled (2026-11-18):** v2 Library Express holds count as accurate. The condition (Q2 shows no CV04 transit
  to Library Express) was met; only `westside` is ambiguous.
- **D2. Approved:** remove the ILL title-text heuristic, in the coordinated implementation (A and C together).
- **D3. Approved:** a separate, scoped accuracy note for v2 ILL data affected by the `AA`/`CY` problem
  (`ill_uncertain_days`, section 5.1).
- **D4. Approved in principle (2026-11-18):** a distinct `unknown_alert` Contract v2 state (section 5.2). It is never a
  confirmed hold or non-hold. Backend and database support (enum, CHECK-constraint migration, summary) ship **before**
  any Collector 1.0.16 produces it; backward compatible (existing states and older Collectors unchanged) and subject to
  the existing latest-record-per-item rule.
- **D5. Provisionally approved:** the branch-settings marker, audited, cleared on rollback, and set to the first full
  local day of verified corrected collection (section 5, B). **Never back-dated (decided 2026-11-19):** the script
  refuses today or earlier.

**Evidence gaps (NBPL)**

- **G1. Closed (2026-11-18):** the production queries ran (section 1.4); the August figure's unit is confirmed
  (item-months). Only Q1's coverage figures are unrecorded, and nothing depends on them.
- **G2.** A controlled ILL hold test (an item trapped for an ILL patron: expect CV01, middle flag Y, bin 0, patron type
  `ILL` for `CY`), and a returning borrowed-ILL item.
- **G3.** The meaning of CV00 / CV03 / CV99 from the ILS vendor's SIP2 documentation.
- **G4.** What the two `101YNY` holds without CV were.
- **G5.** The raw 2026-11-18 records were not reviewed by the engineer; only the summary table in section 1.1.
