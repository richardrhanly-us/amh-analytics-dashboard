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
python -m collector.v2_rules seed --settings <branch_settings.json> --out <root>\config\classification_rules.json   # local file: holds staff names
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
  containing the word "Ill" marks a hold ILL); a parity test compares it with `src/metrics.py`. Rules with staff names stay local,
  are seeded from the current settings, and are held as name HMACs at runtime. `ruleset_id` is a random UUID minted per rules
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

One thing it cannot avoid: importing the v1 collector entry point (`collector.run`) imports the v1 parsers, whose own logger setup creates an
**empty** `logs/parser.*.log` under the *current directory*. That is existing v1 behavior (a test pins that these are the only files and
that they stay empty); run the tool from a scratch directory if it matters.

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
  include Contract v2" (exit 2) rather than crashing -- that fallback is now a safety net, not the normal case. For the exact,
  release-tooling-only NBPL pilot preparation and dry-run acceptance procedure (build-time rules generation, `tools\prepare_v2_pilot.ps1`),
  see [collector-1.0.9-v2-pilot-runbook.md](collector-1.0.9-v2-pilot-runbook.md).
* No historical replay. A future cutover bootstraps the v2 cursor from the v1 cursor.
* Deferred: installer/release integration, `collector_installations` status linkage, key enrollment, retiring v1, dashboard reads.
