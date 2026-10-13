# Collector — Privacy Contract v2

The v2 collector reads the same Tech Logic files as v1 but uploads **only privacy-safe, typed events** to `POST /v2/upload`
(and a free-text-free heartbeat to `POST /v2/status`). Server side: [contract-v2-design.md](contract-v2-design.md).

**Status.** Core collector logic only. It is off by default (`contract_mode` defaults to `v1`) and nothing here deploys, retires v1,
or touches the dashboard. Installer/release integration and installation-status linkage are separate follow-ups (see the end).

## What leaves the machine

| Kind | Fields (and nothing else) |
|---|---|
| `checkins` | `event_key`, `event_time` (UTC), `item_key` (optional), `destination` (slug or `unknown`), `bin` (numeric code or `unknown`) |
| `rejects` | `event_key`, `event_time`, `error_class` (closed enum), `item_key` (optional) |
| `acs_items` | `state` = `hold` \| `non_hold_101` \| `other_code10`, `event_key`, `event_time`, `item_key`; a **hold** also has `destination`, `is_ill`, `is_branch_services`, `is_collection_services`, `ruleset_id` |
| status | `status` (`healthy`\|`degraded`\|`error`), `last_error_class` (enum), `pending_outbox_count` (always 0), `quarantined_count`, two timestamps |

Never leaves: barcode, patron ID/name/type/address, title, call number, collection/shelf codes, raw SIP2 or reject text, staff names,
message codes, raw destination text, exception or server-response text.

**Where the hold classification rules live -- and where they do not.** That list is what a v2 *event* carries. It is not where
the classification *rules* are kept. The collector's local rules file (`classification_rules.json`) is the authority for every
v2 hold: it is classified on this machine and arrives at the server already carrying its three flags. The cloud's
`organization_settings.settings_json["internal_routing"]` -- the same four lists, edited on the dashboard's Admin Settings page --
does not affect a v2 hold at all. It is read only by the dashboard's v1 classifier (`src/metrics.py::build_acs_item_summary`),
for holds from before a branch's cutover, and it is read when a report is made: changing it changes how those older holds are
counted, the next time a report is read. Nothing keeps the two in step. Those cloud lists hold the same patron account names as
the local file -- some of which can be a staff member's own patron card -- so the account names are **not** only on this
machine: they are also in the cloud's settings. (v1 also stored every uploaded row's raw message in the cloud, which carries
patron names; that is v1's, and is not part of v2.)

## The privacy boundary

Only `collector/v2_transform.py` (with the raw modules only it imports: `v2_reader`, `v2_normalize`, `v2_classify`) touches a raw record.
It returns frozen, self-validating event objects (`v2_events.py`), an integer cursor and integer counters. The uploader, status,
quarantine, state, cache and logs receive nothing else, so a raw value has no type to travel in. `tests/test_collector_v2_privacy.py`
proves it two ways: a canary in **every raw field** searched for in **every byte written** (logs, state, status, patron cache,
quarantine, stdout/stderr, HTTP payloads) — raw, case-folded, hex, base64, UTF-16 — and static checks of the import graph and AST
(no exception message is ever logged/printed; the v1 parsers' own loggers are silenced during a v2 parse).

## Keys

* One 32-byte secret, generated on the machine (`secrets.token_bytes`), stored as a **Windows DPAPI machine-scope** blob with a fixed
  application entropy, in a folder whose ACL is **SYSTEM + Administrators only** (inheritance cut). Machine scope lets the Scheduled Task
  (SYSTEM) open it; the ACL is what keeps other local accounts out. The ACL is **verified, and only a verified-protected ACL proceeds**:
  a known-insecure ACL (`secret_exposed`) and an ACL that cannot be read or verified (`secret_acl_unverified`) both **fail closed** — when the
  secret is loaded, when it is initialized (the created secret is removed again if it cannot be verified) and at the start of every run —
  with a fixed code and no exception text. Only `--v2-dry-run`, which uses a throwaway in-memory key, skips this (see *Dry run*).
* Bound to **one server-issued `key_id`** (a non-secret UUID, issued manually with `scripts/issue_ingest_key.py`). A blob for another
  `key_id`, a tampered blob, or a missing one **fails closed** (`secret_key_mismatch` / `secret_unreadable` / `secret_missing`).
* Never in config, environment, logs, support info, payloads or the repo. **No escrow, no recovery:** losing it means a new `key_id`
  (a deliberate break in item continuity).
* Domain separation: HKDF-Expand (RFC 5869) derives five purpose keys — `event`, `item`, `patron`, `name`, `ruleset` — and every
  identifier is an **HMAC-SHA256** under one of them. There is no bare SHA-256 of any identifier (a static test enforces it).
* `item_key = HMAC(K_item, barcode)`. `event_key = HMAC(K_event, canonical form)`, where the canonical form is a versioned text of the
  **v2-safe payload fields only** (`sortview/v2/event/<kind>/1` + `field=value` lines, `~` for absent). It is never derived from a raw
  line, so no prohibited field can influence it (known-answer and invariance tests). A hold's identity is its `state`, `event_time`,
  `item_key`, `destination` and the three classification flags — the classification **result**, so a changed classification is a new event and
  never a conflict. The `ruleset_id` is **provenance only**: it is sent in the hold payload but is **not** part of the `event_key`. A
  *correction* of an earlier hold (below) adds a local `revision` counter to the form, and only when it is greater than 0.

## Setup (operator)

```
python -m collector.v2_rules seed --settings <branch_settings.json> --out <root>\config\classification_rules.json   # local file: holds patron account names
python -m collector.v2_keys init  --config <config.json>     # creates + protects the secret; prints one fixed line
python -m collector.v2_keys check --config <config.json>     # confirms it opens, is bound to the key_id, and its ACL is verified protected
```

```jsonc
"contract_mode": "v2",
"v2": { "key_id": "<server-issued UUIDv4>", "timezone": "America/Chicago" }   // timezone is required; there is no default
```

Optional `v2` settings (all have defaults): `secret_path`, `rules_path`, `patron_cache_path`, `quarantine_path`, `state_path`,
`status_path`, `chunk_lines`, `max_events_per_request` (≤1000), `max_chunks_per_run`, `request_interval_seconds` (2.2, to stay under
the server's rate limit), `patron_ttl_days` (180), `patron_cache_max_rows`, `hold_ledger_days` (90) and `hold_ledger_max_rows`,
`quarantine_max_entries`, `dry_run_tail_bytes`,
`error_after_consecutive_failures`. v2 has **its own** state, status, cache and quarantine files: it never touches the v1 cursor.

## A run

Sources are processed **ACS → check-ins → rejects** (ACS first because it fills the patron cache the guard uses). Each source is read
in bounded chunks of complete lines; a chunk's cursor **and** its patron-cache changes are committed only after every event in it was
delivered or safely quarantined. On any delivery failure nothing of that chunk is committed and the run stops; the source files are the
durable queue, so the next run re-reads and resends the identical events (the server dedups by `event_key`). There is no outbox.

* **ACS.** Code-10 records become `hold` (`101YNY…`), `non_hold_101` (other `101…`) or `other_code10`. Message-64 records only populate
  the local patron cache and produce no event. The classifier reproduces the dashboard's, **including its ILL-title quirk** (a title
  containing the word "Ill" marks a hold ILL); a parity test compares it with `src/metrics.py`. The rules file is local and
  is held as name HMACs at runtime, but it is seeded from a copy of the dashboard's settings, whose own lists remain in the cloud
  (see "Where the hold classification rules live" above). `ruleset_id` is a random UUID minted per rules
  fingerprint — it has no relationship to the rules.
* **A patron profile that arrives after the hold.** The dashboard classifies a window all at once and applies the **latest** message-64 record
  of the item's patron in that window to every hold, whichever side of the hold it falls on. Only three flags can depend on it — `is_ill`
  (patron type or name says ILL), `is_branch_services` and `is_collection_services` (the name is a configured account); the hold's own
  destination, raw-message ILL keywords and `|DA...|` patterns never do. A streaming collector has already sent the hold, so it **corrects**
  it: a small local *hold ledger* (in the patron cache file) keeps, for the latest hold of each item that has a patron, only the item's
  keyed `item_key`, the event time and destination, the patron **HMAC**, the flags it sent and a counter. When a new profile changes a patron's
  classification outcome, those holds are re-sent as ordinary `hold` events for the **same item and the same `event_time`** with the corrected
  flags. The flags are in the event_key, so a correction is a new row and never a 409; the design's "latest = greatest `(event_time, id)`"
  rule makes it win. No patron information is sent, and nothing about the existing contract changes. A correction is sent *before* the
  chunk's own events, so a same-second later record of the same item still outranks it; a hold leaves the ledger when a later 101 record for
  the item supersedes it (or any code-10 record in its own second). The counter (`revision`) exists so that a classification which returns to an
  earlier value (A → B → A) is still a new row and not a duplicate of the first. Ledger rows are kept `hold_ledger_days` (90) and capped at
  `hold_ledger_max_rows`; a profile arriving after that cannot correct the hold. `acs_corrections` counts corrections.
* **Patron cache** (SQLite): patron-ID HMAC, optional name HMAC, two flags, whole-day dates; 180-day *sliding* TTL, row cap. A barcode
  matching a known patron HMAC is dropped and only counted. Patron cards are **never** recognised by length or pattern.
* **Destination / bin.** Same mapping as the v1 parser into slugs; anything unmapped is `unknown` (no pseudonymous label). Bin is a
  1–4 digit code or `unknown`.
* **Rejects** map to the closed enum with the dashboard's precedence; `communication_error` exists in the enum but no text maps to it.
* **Time.** Naive local times are converted with the configured IANA zone. A fall-back (ambiguous) time uses its first occurrence; a
  spring-forward (nonexistent) time is counted; both are aggregate counters. Times before 2000 or >1 day ahead are dropped and counted.
* **409 `event_conflict`.** The reported positions are removed, quarantined (`event_key`, kind, normalised time, fixed reason, date —
  nothing else), and the rest is re-sent; repeated until none remain (the server reports ≤50 per list per response). Quarantined events
  are skipped on every later run. A 422 that pins an event to a list index is handled the same way (`invalid_event`); any other 422 is a
  configuration error.

| Outcome of a run | `status` | `last_error_class` |
|---|---|---|
| completed | `healthy` | — |
| completed, events newly quarantined | `degraded` | `permanent_rejection` |
| completed, a configured source missing | `degraded` | `source_unavailable` |
| 429 / 5xx / timeout / connection | `degraded` (→ `error` after N in a row) | `retryable_infra` |
| 401 / 403 | `error` | `auth_failure` |
| 404 / 405, envelope-level 422, secret/rules/config/state problem | `error` | `configuration_error` |
| 400 / 413, or a batch that cannot be delivered | `error` | `permanent_rejection` |
| anything else | `error` | `other` |

`quarantined_count` is the number of quarantine entries currently retained (entries expire after 90 days; the store is capped).
Logs, status files and the heartbeat contain fixed codes and integers only; exceptions are reported by **type and code location**,
never by message, and are raised outside `except` blocks so no message survives as `__context__`.

### Run and schedule diagnostics

Each heartbeat also carries four diagnostics about the invocation and its Windows Scheduled Task (`collector/v2_schedule.py`).
The dashboard's Pipeline Status panel shows them as *Last Collector Run*, *Next Scheduled Run*, *Latest Run Duration* and
*Collector Schedule*.

| Field | Source and meaning |
|---|---|
| `collector_last_run_at` | Windows Task Scheduler `LastRunTime` for the "SortView Collector" task. It is the scheduler's most recent launch of the task however it was started: a run started with `Start-ScheduledTask` or from the Task Scheduler UI updates it exactly as a trigger does. |
| `collector_next_run_at` | Task Scheduler `NextRunTime`, as Windows reports it. It is never computed from the cadence: if Windows has no next run, none is reported. |
| `collector_run_duration_ms` | How long the invocation took, measured with `time.perf_counter()` (a monotonic clock), never by subtracting wall-clock timestamps. It runs from the start of `collector.run`'s `main()` to the construction of the heartbeat, so it includes configuration and setup, v2 preparation, source processing, delivery, the local status-file write and the Windows Task Scheduler query. It excludes only the heartbeat POST, because a heartbeat cannot report the finished duration of its own request. |
| `collector_schedule_status` | One fixed code, never text: `healthy`, `task_missing`, `task_disabled`, `no_next_run` or `query_failed`. |

**How the schedule is read.** The collector starts Windows PowerShell by its absolute path under the system directory, with
`-NoProfile -NonInteractive -EncodedCommand` and a fixed script that calls `Get-ScheduledTask` and `Get-ScheduledTaskInfo` and
prints one JSON object: booleans, the task state as an integer, and the two times already in UTC and formatted with the
invariant culture. Nothing is parsed from localized `schtasks` output. The query takes a couple of seconds; that time is part
of the invocation and is counted in the duration.

**Nominal** means the query succeeded, the task exists, it is enabled, and Windows reports a next run in the future. A task
state of Running is normal (it is the collector's own run), as is Ready.

| Status | When |
|---|---|
| `healthy` | Nominal, as above. |
| `task_missing` | No task named "SortView Collector" at the scheduler's root. |
| `task_disabled` | The task exists but is disabled. |
| `no_next_run` | The task is enabled but Windows reports no usable future run. |
| `query_failed` | The schedule could not be read: not Windows, PowerShell missing, a timeout, a non-zero exit, or output that is not exactly the expected object. |

The collector fails closed: anything it cannot verify is `query_failed`, and it reports no timestamp from an answer that
failed validation. Nothing the child process prints, and no exception text, is ever logged or sent; the only thing recorded
is the fixed code (`v2 schedule checked | schedule_status=<code>`).

**What the dashboard does with it.** `task_missing`, `task_disabled` and `no_next_run` turn the panel's headline to
"Pipeline Schedule Error" at once. `query_failed` shows an error on the schedule line but only degrades an otherwise healthy
pipeline, since arriving heartbeats show the collector is running. An ingestion error, including an auth failure, keeps its own
headline. The dashboard also checks the clock: a `healthy` schedule whose next run is more than two cadences (30 minutes) in
the past, with no newer heartbeat, is shown as overdue. Two cadences, not one, because the task's
`MultipleInstancesPolicy=IgnoreNew` lets one trigger be skipped legitimately while an earlier run is still going.

**Deployment order.** (1) apply migration `c8d5f2a47e91`; (2) deploy the API and dashboard that accept, store and read the
fields; (3) deploy the new Collector release. A Collector that sends these fields must not be deployed against the old API,
which rejects unknown heartbeat fields and would refuse the whole heartbeat. An older Collector against the new API is fine:
its heartbeat is accepted and the four fields are stored as NULL.

The query runs as whatever account runs the Collector (SYSTEM for the scheduled task, which can read every task). If a
Group Policy execution policy or a missing ScheduledTasks module prevents it, the result is `query_failed`; confirm a
`healthy` result from a real scheduled run on the Collector machine after installing a release that includes this.

## Dry run

`python -m collector.run --config <config.json> --v2-dry-run` transforms a bounded **tail** of each source and prints only
`name=integer` lines. It is **independent of every credential**: it needs no `SORTVIEW_API_TOKEN`, no server URL or `key_id`
registration, and never opens the persistent secret or checks its ACL. It reads only the local configuration it needs — the `sources`
paths, `v2.timezone`, the local rules (`v2.rules_path`, or the default next to `state_path`) and optionally `v2.dry_run_tail_bytes` — and
is dispatched before the v1 configuration is built, so a config without the API url, ids or state paths works.

Its identifiers come from a **throwaway key** generated in memory for the run, so they cannot be compared with anything a real run sent.
It has no network client, commits no cursor, keeps its patron cache and hold ledger in memory only, and opens no state, status, quarantine,
cache, log or secret file of its own. Because everything is in memory and limited to a tail, a profile that lies outside the tail is not
seen, so its classification counts can differ from a full run.

It creates no file anywhere, the working directory included. (Before 1.0.14, merely importing the v1 parsers created an empty
`logs/parser.*.log` under the current directory; they attach no handler of their own now -- see *Parser logging* below.)

## Onsite dry-run acceptance check: identical identities

Two genuinely different events with identical safe fields (same second, same item, same destination and bin) share an `event_key`, exactly as
they did under v1's semantic keys. **Before cutover, run `--v2-dry-run` against real AMH files and read `identical_identity_events`.** It is
always printed, never hidden or zeroed — but a flat "must be `0`" requirement is wrong: the collector's own design intentionally collapses two
documented, tested cases onto one `event_key` (an ACS hold read twice, unchanged, in the same second; a barcode-less
`ils_acs_failure`/`rfid_collision` reject seen twice in the same second — the same safe-field collapse v1's semantic keys already had). Real
NBPL AMH data confirmed exactly this: 91 `identical_identity_events`, all barcode-less reject pairs of those two classes, none in `acs` or
`checkins`.

**The acceptance check is `identity-collision-diag`'s bounded, source/category-aware gate**, not a manual read of the raw count:

```
SortViewCollector.exe identity-collision-diag --config <the same dry-run config>
```

Same safety contract as the dry run (throwaway key, no persistent secret, no network call, no state/cursor write; see
collector/identity_collision_diag.py). Its output ends with a `=== gate ===` block; `identity_collision_gate=pass` means every collision group
was one of:

* an item-keyed ACS hold pair (`state=hold`, group size exactly 2) — the documented repeat-read case, or
* a keyless reject pair whose `error_class` is `ils_acs_failure` or `rfid_collision`, bounded by `keyless_reject_collision_rate <= 0.12` (of
  that source's own event total) and `collision_time_spread >= 0.90` (collisions spread across mostly-distinct seconds, not stacked on a
  handful of timestamps — a stacked pattern instead suggests a frozen/rounded clock).

Any other collision — an item-keyed checkin or reject, a non-hold ACS collision, a group larger than 2, an unrecognized reject class, or a
rate/spread breach — is `identity_collision_gate=fail`, and `collector/deploy/prepare_v2_pilot.ps1`'s onsite preparation tool stops before any
live-v2 action. On a failure, the same command's per-source detail (group-size histogram, distinct item/time counts, category tally) is what
to investigate — no raw-line- or personal-data-derived discriminator is used or planned as a workaround. See
[collector-1.0.9-v2-pilot-runbook.md](collector-1.0.9-v2-pilot-runbook.md) for the full onsite procedure and
`tests/test_collector_identity_collision_diag.py` for this gate's own test coverage (thresholds: `collector/identity_collision_diag.py`'s
`MAX_GROUP_SIZE`, `MAX_KEYLESS_REJECT_COLLISION_RATE`, `MIN_COLLISION_TIME_SPREAD`, `PERMITTED_KEYLESS_REJECT_ERROR_CLASSES`).

Read `identical_identity_events` together with `dropped_patron_card`, `unknown_destination`, `unknown_bin` and `dst_ambiguous`, which the same
run reports.

## Known limits and deferred work

* **Identical identities** — see the onsite acceptance check above. A check-in with no barcode has no `item_key`.
* **Corrections are collector-side and not window-exact.** The dashboard applies the latest profile *inside the window it is showing*
  (Overview: the whole days of the chosen range; Live Today: that one day), so it can classify the same hold differently in different windows;
  no per-event stream can reproduce a window-relative answer. The collector applies the latest profile **it has seen** to holds within the
  ledger horizon. That equals the dashboard's answer for any window that contains the hold and the latest profile. Differences: Live Today
  ignores a profile from an earlier day and the collector uses it; a profile arriving after `hold_ledger_days` corrects nothing; and rule
  changes are not retroactive (the dashboard reclassifies history when its settings change; the collector keeps a hold's own message-derived
  flags as sent and uses the *current* name lists when a later profile corrects it).
* **Replay after a lost cursor.** Replaying is idempotent, except for a patron whose profile *changed* within the re-read range: the replay
  walks the profile through its earlier values again and sends higher-revision corrections. They are new rows, the last one is right, and the
  reduced stream still equals the dashboard's (tested).
* **`ruleset_id` provenance on replay.** `ruleset_id` is stored with a hold for provenance but is excluded from duplicate-equivalence
  checks. Re-sending the same semantic hold under a new `ruleset_id` is therefore an idempotent duplicate: the server returns success,
  stores no additional row, and keeps the originally stored `ruleset_id`. A change to semantic fields such as destination or any
  classification flag still conflicts under the same `event_key`. `tests/test_collector_v2_e2e.py`,
  `tests/test_ingest_v2_api.py` and `tests/test_ingest_v2_postgres.py` pin this behavior.
* **Release packaging.** As of 1.0.6, every v2 module is in `collector.build_release.COLLECTOR_RUNTIME_FILES` and ships in every
  release bundle (source and frozen). `collector/run.py` still reads `contract_mode` itself and imports v2 lazily, so a build that
  ever lacked the v2 modules would still run v1 and answer a `contract_mode: v2` config (or `--v2-dry-run`) with "this build does not
  include Contract v2" (exit 2) rather than crashing -- that fallback is now a safety net, not the normal case. The NBPL pilot's
  preparation and dry-run acceptance procedure is recorded in
  [collector-1.0.9-v2-pilot-runbook.md](collector-1.0.9-v2-pilot-runbook.md); its build-time rules artifact and
  `tools\prepare_v2_pilot.ps1` shipped in releases 1.0.6 - 1.0.13 and are **not in a bundle from 1.0.14 on** (a site's rules come
  only from the API at enrollment).
* No historical replay. A **fresh** v2 install starts at the end of each file (`bootstrap`, see *Fresh-install bootstrap and v2
  preflight* below). An in-place **v1-to-v2 conversion** is different: it needs the v2 cursor carried over from the v1 cursor, there
  is no tool for that yet, and `bootstrap` refuses such a machine rather than seed it at end-of-file.
* Deferred: `collector_installations` status linkage, retiring v1, a replacement-machine workflow, key rotation, and making
  Contract v2 the provisioning default. (The guided setup can install a new machine on Contract v2 as of 1.0.14 -- see *Fresh
  Contract v2 installation* below.)

## Parser logging

The canonical Tech Logic parsers (`agent/parser/*.py`) are shared library code. They log their parse summaries to three named
loggers -- `parser.checkins`, `parser.rejects`, `parser.acs` -- and, as of 1.0.14, attach no handler, create no directory and open
no file: importing them has no effect on the filesystem. The Collector attaches no handler to those loggers, so the summaries
(which include a breakdown of raw destination labels) are written nowhere; under Contract v2 the three loggers are additionally
disabled for the duration of every parse, so nothing could carry them even if a handler were added later.

Before 1.0.14 the parsers configured their own handlers at import and wrote `logs\parser.*.log` under the *current directory*.
That put three files inside a release bundle the first time `preflight` ran from it -- after which the bundle failed its own
manifest verification, so `setup.ps1` could not be run again (or resumed) from the same folder -- and inside the install root on
every preflight and scheduled run, where the installed-manifest check (`tools\verify-install.ps1`) reports any file the release did
not put there. A Collector's only log is the one at its configured `log_path`. A frozen install updated from an earlier release
has its old `<InstallRoot>\logs\parser.*.log` removed by the update, which replaces the whole install root.

## Server-enforced Contract v2 enrollment (onboarding stage A1)

The server, not the installer, decides which contract an enrollment permits.

* `collector_installations.required_contract` (`1` = the legacy contract, `2` = Contract v2) says what an installation must be enrolled
  on. An enrollment code **snapshots** that value when it is generated (`collector_enrollment_codes.required_contract`); what a code
  permits does not follow a later change to the installation.
* `POST /collector/enroll` takes an optional `contract_version`. Absent means the legacy contract, which is what every installer
  released so far sends.

| Code requires | Request says | Result |
|---|---|---|
| 1 | nothing, or 1 | the legacy enrollment, exactly as before: the four fields |
| 1 | 2 | `409 contract_not_permitted` |
| 2 | nothing, or 1 | `409 collector_upgrade_required` -- an installer that cannot do v2 gets no token |
| 2 | 2 | the Contract v2 enrollment |
| any | a version that is not 1 or 2 | `409 unsupported_contract_version` |

A code that is unknown, used, revoked or expired, or whose tenant cannot be enrolled, is still the one generic `400`, whatever was asked
for. A `409` is reached only with a valid code, carries a fixed `code` and sentence, and **never consumes the code**.

**A Contract v2 enrollment** returns the four legacy fields plus `contract_version` (2), `contract_mode` (`"v2"`), `key_id`,
`timezone` and `classification_rules`, and in the same transaction:

* issues the branch's ingest key (`ingest_key_ids`; the same `issue_ingest_key` the script uses). Only the non-secret `key_id` exists
  on the server: the HMAC secret is still generated on the Collector's machine and is never created, returned or stored here;
* records the branch's **initial cutover** (`v2_cutovers`, `set_by = collector-enrollment`) at the moment its **organization** was
  created -- before any event the Collector can send. That row is also what closes the v1 `/upload` route for the branch, so a
  newly enrolled branch cannot upload raw rows even with a hand-edited config.

It is refused with `409 enrollment_not_ready` -- and nothing is written -- unless the site's organization settings hold

* `collector_timezone`: an IANA zone name. There is no default. It configures the Collector only; it is not the zone reports are drawn in;
* `collector_rules`: `{"reviewed_at": <aware ISO time>, "reviewed_by_user_id": <id>}`, the record that an administrator confirmed the
  site's classification lists. Reviewed-and-empty lists are valid; unreviewed lists are not, however full;

and the site's lists are well-formed, the branch has **no active ingest key**, and the branch has **no cutover record of any kind**
(a recorded rollback included). The response never says which of those it was; the server logs a fixed reason code.

`classification_rules` is the site's own rules document, in the schema of the local rules file above, built on the server from the
site's effective settings (`src/services/collector_rules.py`, held to `collector/v2_rules.py` by `tests/test_collector_rules.py`).
It contains patron account names and is never logged.

**Contract 1 is still the server's default.** A new installation is contract 2 only when it is created so explicitly: the
`required_contract` column, `create_collector_installation` and an enrollment request that names no contract all still mean 1.
Super Admin always says which it wants -- a contract selector on both creation forms, which since onboarding stage A3b starts on
**Contract v2** (see *Fresh Contract v2 installation* > *Provisioning*). The legacy contract stays selectable and supported.

**Deployment order.** (1) apply migration `f7b3d2c81a46` (the two `required_contract` columns, and `INSERT` on `v2_cutovers` for the
runtime role -- no `UPDATE`, no `DELETE`); (2) deploy the API; (3) deploy Super Admin. The migration is safe against the previous API.
`v2_cutovers` has no row level security, so that insert is scoped by the enrollment code path itself; database-level scoping is
future hardening.

## Fresh-install bootstrap and v2 preflight (onboarding stage A2)

Two things the collector runtime can do for a `contract_mode: "v2"` config. They are what the guided setup calls for a Contract v2
install as of 1.0.14 (next section); they are also usable on their own. Both are the existing commands; a config
without `contract_mode` (or with `"v1"`) behaves exactly as before, and any other value is a configuration error (exit 2).

### `bootstrap` -- the starting cursor

```
SortViewCollector.exe bootstrap --config <config.json>       # or: python -m collector.bootstrap_state --config <config.json>
```

Without it, a v2 install with no state reads every Tech Logic file from byte 0 and uploads its history. With `contract_mode: "v2"`,
`bootstrap` writes `v2.state_path` (default `<root>\data\v2\state_v2.json`) with each configured source at its **line-safe end**: the
byte after the last complete line, found by the same reader call the v1 bootstrap uses and equal to where a v2 run would stop. A
half-written last record is not skipped; an empty file, or one with no complete line, is seeded at 0.

* It writes the cursor and nothing else -- no patron cache, quarantine, status file, rules or secret -- parses no record and opens no
  network connection. The document is built in memory and written once, atomically: a failure leaves no state file.
* **It refuses, and changes nothing, when starting fresh is not safe:** when the v2 state file already exists, and when it does not
  but the legacy v1 `state.json` does. The second is a machine that has run v1 -- possibly a v1-to-v2 conversion that needs the v1
  cursor carried over -- and seeding it at end-of-file would silently skip every record between that cursor and the end of each
  file. There is no override flag.
* Source names must be `acs`, `checkins` or `rejects` (the rule a run enforces). Each configured source must exist and be readable,
  and is checked again after it was read: one that was replaced or shrank in the meantime fails the bootstrap.

| Exit | Meaning (v2) |
| --- | --- |
| `0` | seeded |
| `1` | a source is missing, unreadable or changed while it was read, or the state file could not be written |
| `2` | configuration: the config, the token, the `v2` section, or a source name |
| `3` | refused: v2 state already exists (`reason=v2_state_exists`) or legacy v1 state exists (`reason=legacy_state_exists`) |

The v1 bootstrap keeps its own codes (`2` for both a configuration error and an existing state file). A caller should rely on the
exit code and on the state file itself, not on the wording of a message. Retrying is safe whenever no state file was written.

### `preflight` -- v2 checks

With `contract_mode: "v2"`, `preflight` runs every existing check and then six more, each calling the same code a scheduled v2 run
does, so it fails where the run would:

| Check | What it proves | Failure code (start of `detail`) |
| --- | --- | --- |
| `v2_config_valid` | the `v2` section loads: `key_id`, `timezone`, limits | `config_invalid` |
| `v2_sources_recognized` | every source name is one v2 knows | `unknown_source` |
| `v2_data_dirs_writable` | the folders of the v2 state, status, patron cache and quarantine | `not_writable` |
| `v2_secret_usable` | the secret exists, its ACL is verified, this process can decrypt it, and it is bound to the config's `key_id` | `secret_missing`, `secret_exposed`, `secret_acl_unverified`, `secret_unreadable`, `secret_key_mismatch`, `secret_store_unavailable`, `secret_check_failed` (any other error) |
| `v2_rules_valid` | the local rules file loads and validates | `rules_missing`, `rules_invalid`, `rules_check_failed` (any other error) |
| `v2_ingest_authorized` | the server has v2 ingest enabled and the configured key is active for the token's tenant | `authentication_failed` (401), `ingest_key_not_authorized` (403), `v2_unavailable` (404/405), `status_request_invalid` (422), `server_unavailable` (429, 5xx, no response), `unexpected_response` (any other status) |

A check that could not run because an earlier one failed reports `not_evaluated`. Exit codes are unchanged (`0` all passed, `1` a
check failed, `2` the config did not load or names an unsupported contract), and `--output` still writes one `{name, passed, detail}`
row per check: rely on `name`, `passed` and the code, not on the prose. Preflight never creates or replaces a secret, and prints
neither the `key_id`, the secret, a rule nor a server response. It runs the same checks interactively and as SYSTEM
(`tools\preflight-system.ps1`); because the secret's folder is Administrators + SYSTEM only, the interactive run must be elevated.

**Two server probes.** `POST /upload-pipeline-status` is sent exactly as before: it is what moves a `collector_installations` row
to `active` and stamps `installed_at` / `last_seen_at`, which `/v2/status` never touches. `POST /v2/status` follows only when that
probe passed **and** every local v2 check passed. No event is uploaded.

**The v2 probe writes a heartbeat.** `/v2/status` stores a full snapshot on the ingest key, and that snapshot is what the dashboard
shows for a branch on Contract v2. When a run has already written `status_v2.json`, preflight re-sends that run's health, error
class, last success, quarantine count and start time (the run and schedule diagnostics are omitted, so the server holds NULL for
them until the next scheduled run). On a fresh install there is nothing to re-send, so it sends the smallest valid snapshot,
`healthy`. **That heartbeat means "v2 preflight passed", not "a production run has happened":** a fresh install can show a healthy
state before its task has run once. A neutral, non-reporting validation endpoint would remove this and is not built.

## Fresh Contract v2 installation (onboarding stage A3a)

Collector **1.0.14**'s guided setup (`setup.ps1`, frozen bundles) installs a new machine directly on Contract v2 when the
enrollment code was generated for a **contract 2 installation**. Nothing about an existing install changes, and the same
bundle still installs a contract 1 code on the legacy contract. **Contract 2 is the provisioning default (onboarding stage
A3b):** Super Admin's contract selector starts on Contract v2, and the legacy contract is chosen explicitly (see
*Provisioning* below). The steps and every stop are the script's own text; this is the order and the guarantees.

| Step | What happens | If it fails |
| --- | --- | --- |
| 0 - 4 | elevation; the bundle verified against its manifest; **a clean machine**; the three source files exist; the API is reachable; the setup folder can be created, restricted and written | nothing was changed; the code was not requested |
| 5 | the code is redeemed with `contract_version: 2` | see *Refusals* |
| 6 | the non-secret enrollment details and the site's **classification rules** are saved to `<DataRoot>\setup` and verified -- **before** anything else | stop; SortView support |
| 6b | the API token is stored (`api_token.dpapi`) and verified | stop; SortView support issues a replacement token, then setup resumes |
| 7 | `install.ps1` installs the runtime and writes a v2 config (`contract_mode`, `v2.key_id`, `v2.timezone`), atomically | resume |
| 7b | the staged rules are installed as `<DataRoot>\config\classification_rules.json` | resume |
| 8 | `tools\finish-install.ps1`: the local secret (`v2-key check` / `init` / `check`), the interactive preflight, the SYSTEM preflight, `bootstrap`, the task registered **Disabled** | resume |
| 9 | the Scheduled Task is **enabled** -- the last action, with no question | the task stays Disabled |

* **A clean machine.** A first install is refused, before the code is requested, if the machine already has a Collector
  runtime, config or Scheduled Task, **or** an API token, a Contract v2 secret, a state file of either contract, or a
  classification rules file. `tools\uninstall.ps1` keeps the data root, so these outlive an uninstall. Nothing is adopted,
  replaced or deleted; replacing a stored token is not offered.
* **The rules are the server's.** The document in the enrollment response is written exactly as received (UTF-8, no BOM,
  atomically), read back and compared before it is trusted; nothing is merged in and no default is added. It is never
  printed, and it is not in the resume record -- only the SHA-256 of the staged file is, which is what ties that file to the
  enrollment. No release bundle carries any site's rules (1.0.6 - 1.0.13 shipped `pilot\classification_rules.json` and
  `tools\prepare_v2_pilot.ps1`; 1.0.14 ships neither).
* **The secret is the machine's.** `v2-key init` generates it locally; setup never sees it and it is never sent. An existing
  secret is reused only if `v2-key check` accepts it for this installation's key id; one that it does not accept is never
  overwritten.
* **The time zone is the administrator's.** `v2.timezone` is the IANA zone recorded for the site in Super Admin. Setup does
  not compare it with the Windows time zone.
* **The task.** No task exists until the secret, both preflights and the bootstrap have succeeded; it is registered
  Disabled and enabled only as setup's final action. A failure at any earlier point leaves no active task. Setup never
  starts a run: the first one is the task's next 15-minute trigger, and it reads from the cursor seeded at step 8.
* **A legacy code.** The first request is refused `contract_not_permitted` without using the code; setup sends the legacy
  request (no `contract_version`) once and follows the legacy path unchanged -- including its question before enabling
  the task. Nothing else is ever retried.

### Refusals

The API's 409 refusals are read by their fixed `code` only (the rest of the body is never shown). **None of them uses the
enrollment code**, and setup says so:

| `code` | Meaning for the technician |
| --- | --- |
| `contract_not_permitted` | a legacy code: retried once on the legacy contract (above) |
| `collector_upgrade_required` | this bundle is too old for the code: use the current bundle with the same code |
| `enrollment_not_ready` | the site is not prepared (time zone, reviewed lists), or the branch already has a Contract v2 Collector |
| `unsupported_contract_version` | the bundle and the server do not agree on a contract |

A rate limit (429) and a redirect never reached the API and are also reported as "not used".

**Unknown results.** Every other failure of the enrollment request leaves it unknown whether the server enrolled the machine:
no response at all (a timeout, a reset, a connection closed after the request was sent), a 5xx, an unexpected status, an
unrecognized 409, or a 200 that could not be read. For these setup never says the code "was not used" and never advises getting
a new one. It says the result is unknown and to **run `setup.ps1` again with the same code**; if that code is then refused as
invalid or already used, the first request may have completed and the next step is **SortView support**, not another code. A
400 means only that *this* request enrolled nothing, and its advice puts that case first. Setup never retries an enrollment
by itself. A damaged resume record of a Contract v2 enrollment is treated the same way: setup stops, changes nothing, and
directs to support.

### Resume

A Contract v2 enrollment creates the branch's v2 identity on the server -- its ingest key and its cutover -- exactly
once, and a second code for the branch is refused. So after step 6 an interrupted setup is **finished from disk**: run
`setup.ps1` again and accept the resume. It makes no enrollment request and asks for no code; it reuses the stored token,
the staged or installed rules, a valid existing secret and a valid existing v2 cursor, and completes only what is
unfinished. It refuses (and changes nothing) if the installed config, or a rules file, is not the saved enrollment's.

The resume record (`<DataRoot>\setup\enrollment-recovery.json`, Administrators and SYSTEM only) holds, for Contract v2:
the three IDs, the API address, the release version, `contract_version`, `contract_mode`, the non-secret `key_id`, the
time zone and the staged rules file's SHA-256. It never holds the enrollment code, the API token, the HMAC secret or any
rule. A legacy enrollment's record is unchanged.

**What still needs SortView support** (a new enrollment code is not a recovery path for any of these):

* the enrollment succeeded on the server but its response never reached the machine;
* the API token could not be stored after enrollment -- support issues a replacement token for the installation, it is
  stored with `tools\set-api-token.ps1`, and setup resumes;
* the enrolled machine is lost or replaced.

### Provisioning

Super Admin's two creation paths -- *Provision Library* and *Manage Libraries > Add installation* -- have an explicit
contract selector. **Its default is 2 (Contract v2)** since onboarding stage A3b; 1 (legacy) stays selectable and needs
none of what follows. Contract 2 requires the site's Collector time zone and its reviewed classification lists (and, when
provisioning, a confirmation that the transit destinations entered are complete, because for Contract v2 they become its
classification rules), and is labelled as needing Collector 1.0.14 or later. The choice is made once, when the
installation is created; an existing installation's contract cannot be changed. A branch has one Contract v2 Collector:
adding a second or replacement installation to a branch that has already enrolled on Contract v2 is not supported yet.

*Provision Library* (A3b):

* The contract selector is the first field, and the form is submitted only by its button -- pressing Enter in a field
  does nothing.
* Everything is checked before anything is created: the names, a chosen plan, and for Contract v2 the three Collector
  Site Settings entries (marked `*`). Every problem is listed at once and no organization, installation or operational
  identity exists until there is none.
* The plan is chosen from the active rows of `plans`; none is pre-selected and none can be typed. (`trial` is a
  subscription status, not a plan.)
* No transit label is pre-filled. Destinations are entered one per line and each is stored with its **canonical key**,
  `services.routing_destination.destination_key(label)`: the one key the reports look a destination up by, and always a
  valid destination slug (a letter first, at most 32 characters -- a label that starts with a digit gets `d_` before
  it), so the rules the Collector is given carry that same key. Two different labels that share a key are refused
  where they are saved (here, in *Collector Site Settings*, and in Admin Settings) rather than treated as one.

*Manage Libraries > Collector Site Settings* (A3b) shows whether an existing library is ready for Contract v2 and
repairs it: the Collector time zone, the enabled transit destination labels, and a review confirmation that records the
signed-in Super Admin and the time. It reads and writes the **organization's** own settings -- where the enrollment
reads them -- and leaves every other setting as it is. The four Internal Routing lists hold patron account names: this
page shows only how many entries each has, and they are edited in the library's Admin Settings.

Admin Settings no longer offers another library's values where an organization has none: a missing home label or
destination list is shown blank, and an empty destination row is not saved.
