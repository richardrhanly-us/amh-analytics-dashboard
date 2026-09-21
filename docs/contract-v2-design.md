# Privacy Contract v2 -- server design (Step 3: additive server support)

Status: **approved design; Step 3 implements the server side only.** The collector, the dashboard and data-loader, v1 retirement
and any purge are later steps and are not touched here.

Contract v2 lets a collector upload operational events that carry **no raw SIP2, no patron ID or card value (or any derivative
of them), no raw item barcode, no title and no free-text error/status text**. The server accepts v2 *alongside* v1; nothing
about v1 changes.

## 1. Decisions

| Area | Decision |
|---|---|
| Architecture | Parallel v2 tables and a separate `POST /v2/upload` (events) and `POST /v2/status` (heartbeat). `/upload`, the v1 models, v1 tables, v1 triggers and v1 behaviour are unchanged. No flag-day. |
| New tables | `checkin_events`, `reject_events`, `acs_item_events`, `ingest_key_ids`. No other table is created or altered. (Step 3 created the ACS table as `acs_hold_events`; the amendment in section 9 evolves it into `acs_item_events`.) |
| Tenant scope | v2 payloads contain no `customer_id` / `branch_id`. Scope comes only from the authenticated agent token. Every v2 model is `extra="forbid"`, so a payload that names a tenant field is a 422. |
| `key_id` | Server-issued, opaque, a lower-case UUIDv4. Registered per `(customer_id, branch_id)` in `ingest_key_ids`, which stores **no HMAC secret**, only a non-secret algorithm identifier (`hmac-sha256-v1`) and a lifecycle state (`active` / `retired`). Unknown, retired and wrong-tenant keys are rejected identically. |
| Identity | `event_key` and `item_key` are opaque keyed-HMAC identifiers generated locally (64 lower-case hex). The cloud never receives the HMAC secret. Plain SHA-256 barcode hashing is prohibited. `source_event_id` does not exist in v2. |
| Dedup | Unique per table on `(customer_id, branch_id, key_id, event_key)`. An identical resend is idempotent. The same identity with different content is a **conflict**, never silently ignored (section 5). |
| Time | Offset-aware ISO-8601 only, stored as `TIMESTAMPTZ` (UTC). Naive timestamps, epoch numbers and dates are rejected. |
| `destination`, `bin` | Not raw AMH labels: normalized slugs produced locally. Never empty; the explicit value `unknown` is used where needed. No human-readable / cloud labels in Step 3. |
| `ruleset_id` | Opaque random UUIDv4. Not readable text, not a hash of the ruleset's contents. |
| `error_class` | Closed enum: `item_not_found`, `ils_acs_failure`, `rfid_collision`, `configuration_error`, `routing_error`, `communication_error`, `other`, `unknown`. No free-text fallback. |
| Bounds | At most **1000 events in total** across `checkins`, `rejects` and `acs_items`, and at most 1000 per list. A byte-size guard covers both `Content-Length` and chunked (no `Content-Length`) requests. |
| Heartbeat | `POST /v2/status`: typed and allowlisted fields only (section 4). No free-text `last_error`, no open dictionaries, no exception strings, no HTTP/response previews. |
| 422 handling | A caller-supplied unexpected field **name** is never echoed into a response or a log line, and neither is any request value. |
| Gating | `SORTVIEW_V2_INGEST_ENABLED`, default **off** (only the exact value `true`, any case, turns it on). A v2 request needs the global flag **and** an active matching `key_id`. |
| Persistence | The v2 tables physically lack `barcode`, `title`, `patron_id`, `raw_message`, any free-text message and `source_event_id`. Each table has an explicit insert-column list; `model_dump()` is never used as SQL parameters. |

## 2. Request contract

Both endpoints require `Authorization: Bearer <agent token>` (same token, same 401/403 gates as v1: valid, active, tenant usable,
bound installation usable). `POST /v2/upload`:

```
{
  "contract_version": 2,
  "key_id": "<uuidv4>",
  "checkins":   [ CheckinEvent, ... ],
  "rejects":    [ RejectEvent, ... ],
  "acs_items":  [ AcsItemEvent, ... ]      // a hold, or a non-hold record that can retract a hold (section 9)
}
```

| Model | Fields (all required unless marked optional) |
|---|---|
| `CheckinEvent` | `event_key`, `event_time`, optional `item_key`, `destination`, `bin` |
| `RejectEvent` | `event_key`, `event_time`, `error_class`, optional `item_key` |
| `AcsItemEvent`, `state = hold` | `state`, `event_key`, `event_time`, `item_key`, `destination`, `is_ill`, `is_branch_services`, `is_collection_services`, optional `ruleset_id` |
| `AcsItemEvent`, `state = non_hold_101` or `other_code10` | `state`, `event_key`, `event_time`, `item_key` **only** |

| Field | Rule |
|---|---|
| `contract_version` | the integer `2` (strict; `true`, `"2"` and `2.0` are rejected) |
| `key_id`, `ruleset_id` | canonical lower-case UUIDv4 |
| `event_key`, `item_key` | `^[0-9a-f]{64}$` |
| `event_time` | ISO-8601 **with** a UTC offset (`Z` or `+hh:mm`), 2000-01-01 up to one day ahead of now |
| `destination` | `^[a-z][a-z0-9_]{0,31}$` |
| `bin` | `^[a-z0-9][a-z0-9_]{0,15}$` |
| `error_class` | the closed enum above |
| `is_*` | strict booleans; independent (more than one may be true) |
| lists | each at most 1000; **total** across the three at most 1000; a request with no events is a 400 |

All models are `strict`, `frozen` and `extra="forbid"`. A raw AMH label (`Westside`, `Library Express`,
`DA(AH) TS(AH)-CATALOGING`, `No Agency Destination`) fails the slug patterns. A raw label that happens to be a valid slug
(`main`) is indistinguishable from a normalized one; the collector is responsible for normalizing.

Responses: `200 {"status": "success", "contract_version": 2, "<kind>_received", "<kind>_inserted", "<kind>_duplicates"}`
for each of `checkins`, `rejects`, `acs_items`. Errors: 401/403 (token, tenant, key), 404 (flag off), 409 (conflict), 413
(too large), 422 (validation; locations and kinds only), 400 (empty).

## 3. Database (Alembic `d3f1a8c95b27`, head after `b4e91d7a3c58`)

Additive only: four `CREATE TABLE`s and their indexes; no v1 table, trigger, index or constraint is altered. All timestamps
are `TIMESTAMPTZ`.

* **`ingest_key_ids`**: `key_id` (unique, UUIDv4), `customer_id` / `branch_id` (FKs to `customers` / `branches`), `algorithm`
  (`hmac-sha256-v1`), `status` (`active` / `retired`, with `retired_at` set exactly when retired), `created_at`, plus the
  latest v2 heartbeat snapshot (section 4). It holds no key material.
* **`checkin_events`**: `customer_id`, `branch_id`, `key_id`, `event_key`, `event_time`, `item_key` (nullable), `destination`,
  `bin`, `received_at`.
* **`reject_events`**: `customer_id`, `branch_id`, `key_id`, `event_key`, `event_time`, `error_class`, `item_key` (nullable),
  `received_at`.
* **`acs_hold_events`** (Step 3; renamed and extended into `acs_item_events` by the amendment, see section 9): `customer_id`, `branch_id`, `key_id`, `event_key`, `event_time`, `item_key`, `destination`,
  `is_ill`, `is_branch_services`, `is_collection_services`, `ruleset_id` (nullable), `received_at`.
* **Constraints mirror the API** where practical: format `CHECK`s on `key_id`, `event_key`, `item_key`, `destination`, `bin`,
  `ruleset_id`; `error_class` matches a slug pattern (the closed enum lives in the API, so adding a class needs no migration).
* **Indexes**: the unique dedup index on `(customer_id, branch_id, key_id, event_key)`; `(customer_id, branch_id, event_time)`
  for the dashboard's usual scoping; a partial `(customer_id, branch_id, item_key)` where `item_key` is not null.
* The event tables deliberately have **no foreign key to `ingest_key_ids`**: the key is validated once per request, and the
  hot insert path stays free of an extra check. Downgrade drops the four tables (destructive once v2 data exists).

## 4. Heartbeat (`POST /v2/status`)

```
{ "contract_version": 2, "key_id": "<uuidv4>", "status": "healthy|degraded|error",
  "last_error_class": null | "retryable_infra|auth_failure|permanent_rejection|source_unavailable|configuration_error|other",
  "pending_outbox_count": null | int (0..10,000,000), "quarantined_count": null | int (0..10,000,000),
  "oldest_pending_event_at": null | ISO-8601 with offset, "last_success_at": null | ..., "watcher_last_active_at": null | ... }
```

`status` is the collector's **overall** state: `healthy`, `degraded` or `error`. A *kind* of failure is never a status:
`auth_failure`, for example, is a `last_error_class`. Both enums are closed and frozen (pinned by tests); no other value and no
free text is accepted in either.

Every heartbeat is a **full snapshot** of these fields (an omitted optional field is stored as NULL), stored on the matching
`ingest_key_ids` row together with a server-side `last_heartbeat_at`. It requires the flag and an active matching key, like
an upload. It never writes `pipeline_status`, `collector_installations` or any v1 table, so the v1 dashboard does not see a v2
collector's health until the dashboard step reads it. Body limit 16 KiB.

## 5. Dedup and conflict behaviour

* The dedup identity is `(customer_id, branch_id, key_id, event_key)` per table. It replaces v1's semantic keys, which depend
  on the barcode v2 no longer has.
* **Identical resend** (same identity, same content): accepted; counted under `_duplicates`; nothing changes.
* **Same identity, different content** (any of `event_time`, `item_key`, `destination`, `bin`, `error_class`, `state`, the `is_*`
  flags, `ruleset_id`): a **conflict**. Also detected between two events inside one request. The request is **rejected as a
  whole with `409` and nothing is stored** (all-or-nothing, as v1's single transaction). The body is
  `{"code": "event_conflict", "detail": "...", "conflicts": {"<kind>": [<request indexes>]}}`: list positions only, no keys
  or contents. The log line carries the token id, tenant ids, `key_id` and per-kind counts, never an event's content.
* Rotating `key_id` yields new `event_key`s for the same physical events, so a replay across a rotation duplicates rows.
  Rotation must be coordinated with the collector's read cursor.

## 6. Compatibility and rollout

1. Apply the migration (purely additive, safe against the running API), on a disposable PostgreSQL first.
2. Deploy the API with `SORTVIEW_V2_INGEST_ENABLED` unset: a `POST` to either v2 route answers `404` (see 8.1 for the one
   difference from an unknown path).
3. Issue a key for a **test** tenant (`scripts/issue_ingest_key.py`, dry run by default) and exercise v2 with synthetic payloads.
4. Later steps (not Step 3): the v2 collector, the dashboard read path, v1 retirement, historical purge.

Rollback: unset the flag; if needed, downgrade the (empty) v2 tables. v1 collectors are unaffected throughout, including
while the flag is on.

## 7. Known limits (not weakenings, but not enforceable by the server)

* **HMAC versus plain SHA-256 is indistinguishable** to the server (both are 64 hex characters). The mitigations are the key
  registry (only server-issued keys are accepted, so a collector cannot even name a key it invented) and collector-side
  known-answer tests. The server can never verify how a key was derived.
* `destination`, `bin` and `ruleset_id` are format-checked, not semantically checked: a slug that is a real patron or staff
  name would pass. `ruleset_id` being a random UUID and never derived from the ruleset is what keeps staff names in the
  classification rules (see `src/branch_settings.json`) out of the cloud.
* The v2 byte-size guard protects the v2 routes only. The pre-existing `MaxBodySizeMiddleware` still trusts
  `Content-Length` for every route and raises on a non-numeric header; changing it would change v1 and is out of scope.
* The dashboard's "no agency destination" list shows title and barcode; v2 cannot supply either. That is a product decision
  for the dashboard step.

## 8. Implementation notes and deviations (as built in Step 3)

Everything above was implemented as approved. These are the places where the build made a choice the approval did not spell
out, or differs from a sentence in this document as first written. Each is deliberate and tested.

1. **Flag off = `404` for `POST`, not for every method.** Both v2 routes are always registered and check the flag first
   (before reading the body), so tests and operators can toggle it without re-importing the app. A `POST` answers exactly
   `404 {"detail": "Not Found"}`, but a `GET` on a v2 path answers `405` where an unknown path answers `404`. That discloses
   only that the path exists.
2. **The heartbeat is stored on `ingest_key_ids`** (its latest snapshot plus `last_heartbeat_at`), not in a fifth table and
   not in `pipeline_status`. The approved table list is unchanged and no v1 table is written. Consequence: the v1 dashboard
   and any monitor that reads `pipeline_status` will not see a v2 collector's health until the dashboard step reads it.
3. **The heartbeat's value sets were proposed here**, because the approval named the kinds of field and not the values, and
   `status` was then corrected in review: it is the collector's *overall* state, `healthy | degraded | error`, and
   `auth_failure` is an error *class*, not a state. **`last_error_class` is exactly** `retryable_infra | auth_failure |
   permanent_rejection | source_unavailable | configuration_error | other` (frozen; pinned by tests). Counters
   `pending_outbox_count`, `quarantined_count` (0..10,000,000); timestamps `oldest_pending_event_at`, `last_success_at`,
   `watcher_last_active_at`. `collector_version` was **not** included (not an approved field). No new free-text field exists.
4. **Timestamp spelling is exact**: `YYYY-MM-DDTHH:MM:SS[.f to 6 digits]` followed by an upper-case `Z` or `+hh:mm`/`-hh:mm`.
   A lower-case `t`/`z`, a space separator, basic format (`20260921T100000Z`), an offset without a colon, epoch numbers and
   `datetime` objects are all rejected. The accepted window is 2000-01-01 up to one day ahead of now. The database cannot
   enforce "offset-aware" (PostgreSQL's `TIMESTAMPTZ` accepts an offset-less literal in the session time zone); the API is the
   offset-aware gate, and the column type is what guarantees an unambiguous instant once stored.
5. **`contract_version` is the integer `2` exactly.** `Literal[2]` alone accepts the float `2.0` (Python's `2.0 == 2`), so a
   before-validator requires the type to be exactly `int`; `true`, `"2"` and `2.0` are rejected.
6. **A conflict rejects the whole request** with `409` and stores nothing (all-or-nothing, like v1's single transaction). A
   collector must therefore treat `409` as a permanent, per-batch condition and use the reported positions (at most 50 per
   list) to quarantine the offending events rather than retry the batch. The identity, not the content, is what an identical
   resend matches on; an in-request duplicate with identical content is stored once and counted under `_duplicates`.
7. **`key_id` is globally unique** in the registry (a single unique index), in addition to being tenant-scoped at lookup.
8. **The 422 hardening is in the shared helper** (`safe_validation_errors`): for an `extra_forbidden` error the last element
   of the location, the caller's own field name, is always `<key>`. v1's models never produce `extra_forbidden`, so v1's
   validation responses are unchanged; the server's own parent field names are still reported.
9. **`authenticate_agent` was split, not changed.** One private core takes an optional expected scope. `authenticate_agent`
   (v1) passes the request's ids, so its checks, their order and its side effects are identical; `authenticate_agent_token`
   (v2) passes none. v1's own suites pin this.
10. **Request-size limits** are constants (1 MiB for `/v2/upload`, 16 KiB for `/v2/status`), not environment variables, so a
    misconfiguration cannot silently loosen them. Both are enforced for a declared `Content-Length` and for chunked bodies.
11. **`scripts/issue_ingest_key.py`** issues (`issue`) and retires (`retire`) keys; dry run by default. There is no
    reactivation: a retired key is replaced by a new one.

### 8.1 Key issuance handles only the non-secret `key_id`

The registry and the tool manage **one thing: an opaque, non-secret identifier**. The server never generates, stores, prints,
receives or manages a collector's HMAC secret.

* The only value the tool creates is `uuid.uuid4()`: a random label, not key material. It is not derived from, and cannot be
  used to derive, any secret. Neither the tool nor the service imports `hmac`, `secrets`, `hashlib` or a crypto library.
* `ingest_key_ids` has no secret, key-material, salt or seed column (checked on the migration DDL and on a real database).
* The tool prints the `key_id`, the tenant ids and a sentence saying where the secret lives (on the collector). It prints no
  URL and no credential.
* No request model has a field that could carry a secret (`extra="forbid"`; a `hmac_secret`, `secret`, `key_material` or
  similar field is a 422), and no API response echoes anything but counts, fixed codes and request positions.
* Because only server-issued `key_id`s are accepted, a collector cannot even name a key it invented, so a secret cannot be
  smuggled in as a `key_id`.

Verification: `tests/test_ingest_v2_models.py`, `tests/test_ingest_v2_api.py`, `tests/test_ingest_v2_migration.py` and
`tests/test_issue_ingest_key.py` run everywhere. `tests/test_ingest_v2_postgres.py` is opt-in (`SORTVIEW_TEST_POSTGRES_URL`,
a local non-production server) and runs the real migration, the real constraints, the unique index, concurrent writers, the
endpoints, an upgrade over existing v1 data with a before/after comparison of every v1 object, and a downgrade cycle.

## 9. Amendment: the ACS item-event stream (preserving retraction semantics)

Alembic `e5a2c7b93d14`, on top of Step 3's `d3f1a8c95b27` (which is not rewritten). It is a server-contract amendment; it changes no
dashboard semantics and no v1 route or table.

### 9.1 Why Step 3's hold-only shape was not enough

The dashboard decides whether an item is a hold from the item's **latest** ACS record, and does so differently in two places
(traced and executed against the real code):

* **Overview** (`metrics.build_acs_item_summary`, over the reporting window) considers only `101` records, keeps the latest per
  item, and counts the item only if that latest record is `101YNY`.
* **Live Today** (`build_live_context`) first keeps the latest **code-10** record per item (any `10x`, including `100…`), and only
  then applies the 101 filter.

So a later non-hold record retracts an earlier hold, and the two paths disagree about which later records count. A hold-only stream
cannot represent that: the cloud would never learn of the later record. Message-64 patron records affect neither path and stay local.

### 9.2 The contract

`acs_items` replaces `acs_holds`. Each item has a closed, **derived** `state` (the raw message code never leaves the collector):

| `state` | Meaning | Fields |
|---|---|---|
| `hold` | a `101` record that is hold-positive (`101YNY`) | `state`, `event_key`, `event_time`, `item_key`, `destination`, `is_ill`, `is_branch_services`, `is_collection_services`, optional `ruleset_id` |
| `non_hold_101` | a `101` record that is not a hold | `state`, `event_key`, `event_time`, `item_key` |
| `other_code10` | any other code-10 record: ignored by Overview, but takes part in Live Today's latest-wins rule | `state`, `event_key`, `event_time`, `item_key` |

The request model is a discriminated union on `state`. A non-hold carries **no** destination, flag or ruleset, and `extra="forbid"`
means none can be sent (not even `null` or `false`): no dummy value is invented to fit the hold shape. A hold must carry every
derived field. An unknown or missing `state` is a 422 (`union_tag_invalid` / `union_tag_not_found`) with a fixed message and no echo
of the submitted tag. There is no `state` for message-64 records, so they cannot be sent.

`event_key` remains an opaque HMAC over a deterministic versioned canonical form of the **same privacy-safe fields** (for a
non-hold: version/kind, `event_time`, `item_key`, `state`; for a hold: those plus destination, the three flags and the ruleset id
or none). The raw line, barcode, patron identifiers, title and raw message code never take part. The server does not compute it and
cannot verify it (section 7); the collector step will. Because `state` is part of the identity, changing a record's state under one
`event_key` is a conflict (409), not a silent overwrite.

### 9.3 The database

`acs_hold_events` becomes `acs_item_events`: the table, its sequence, primary key, foreign keys, constraints and indexes are renamed;
`state` is added (existing rows are all holds); `destination`, `is_ill`, `is_branch_services` and `is_collection_services` become
NULLable; two `CHECK`s enforce the closed `state` and the **shape of each state** (a hold has all four fields; a non-hold has none of
them and no `ruleset_id`), so even a writer that skips the API cannot store a placeholder. The identity index
`UNIQUE (customer_id, branch_id, key_id, event_key)` is unchanged. The `(customer_id, branch_id, item_key)` index becomes
`(customer_id, branch_id, item_key, event_time)`, the shape "latest record per item" reads.

Downgrade refuses to run if any non-hold row exists (it would destroy retraction data); otherwise it restores Step 3 exactly.
No v1 table is touched.

**Deploy order.** The amendment is safe because v2 ingest is off (`SORTVIEW_V2_INGEST_ENABLED` unset) until the collector cutover. Apply
the migration and deploy the matching API code together, migration first, with the flag off throughout: the previous API code
writes the old table name.

### 9.4 What the later dashboard step must do (the specification the stream supports)

"Latest" means the greatest `(event_time, id)`. `id` is assigned in insert order, which is the order the collector sent the events,
which is source-file order, so equal-time ties are deterministic (the dashboard's own pandas sort is not guaranteed stable for exact
ties; the stream is).

* **Overview**: take `hold` and `non_hold_101` events in the reporting window; the latest per item wins; count an item only when
  that latest state is `hold` (public / ILL / branch services / collection services from that event's own flags). Ignore `other_code10`.
* **Live Today**: take all three states for the latest date; the latest per item wins; count only a `hold`.

`tests/test_ingest_v2_api.py` (section 11) proves this: each investigated sequence is run through the real Overview and Live Today code
and, after `POST /v2/upload`, through a reference reduction of the persisted stream; every outcome is equal. The sequences are hold
followed by a later non-hold, a later hold, or a later other-code-10 record; a non-hold followed by a hold; a hold followed by a
message-64 record; and an ILL hold followed by a later non-hold or a later non-ILL hold. Overview windows are covered too.

### 9.5 Privacy guarantees of the item stream

The stored rows contain only: the tenant ids and `key_id` (server-derived), `event_key` and `item_key` (keyed HMACs), `event_time`,
the closed `state`, and, for a hold only, a normalized destination slug, three booleans and an opaque random ruleset id. There is no
patron id, name or card derivative, no raw SIP2, no raw barcode, no title, no raw message code and no free text: the model forbids
extra fields, the API forbids dummy values, and the table has no column that could hold any of them.

### 9.6 Compatibility

The wire name `acs_holds` and the table name `acs_hold_events` are gone; nothing consumed them (no collector or dashboard code
referenced them, and v2 is not enabled anywhere). No alias for the old names was added: the contract is kept clean for the long term.
