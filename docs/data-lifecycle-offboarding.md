# data lifecycle and tenant offboarding

How a library (a tenant) stops using SortView, what happens to its data, and
what "deleted" does and does not mean. The machine-readable version of the
table-by-table policy is `src/services/data_lifecycle_policy.py`; the purge
tool reads its delete plan from there, and a test fails if a database table
is not classified in it.

## three separate things

| | Suspension | Offboarding (access cutoff) | Data purge |
|---|---|---|---|
| What it is | A reversible pause | The permanent end of service | Deletion of the tenant's rows |
| Organization status | `suspended` | `cancelled` (terminal) | row deleted |
| Reversible | Yes: Reactivate | Not from the product | No |
| Who | Super Admin | Super Admin | SortView operator, as the table owner |
| How | Manage Libraries: Suspend Library | Manage Libraries: Offboard Library (permanent) | `scripts/purge_tenant_data.py` |
| Deletes data | No | No | Yes |
| Changes tokens, installations, keys | No | Revokes all of them | Deletes them |
| Dashboard access | Read-only | None | None |

They are deliberately three code paths. Suspension
(`set_library_active_status`) never reaches offboarding, offboarding
(`offboard_library`) never deletes, and the runtime database role cannot
delete at all.

**Order is fixed:** a tenant is purged only after it has been offboarded. The
purge tool refuses a tenant that is active, trial or suspended.

## suspension

Unchanged by this work. `organizations.status` becomes `suspended` and
nothing else is written: data, operational identity, installations,
subscriptions and agent tokens are all preserved. Collector uploads are
rejected while suspended; the dashboard is read-only. Reactivating sets
`active` and never reactivates a token.

## offboarding: the access cutoff

Super Admin, Manage Libraries, **Offboard Library (permanent)**, with the Org
Slug typed as confirmation. In one database transaction:

| Surface | What happens |
|---|---|
| `organizations` | `status = 'cancelled'` |
| `agent_tokens` | Every active token of the tenant is deactivated: tokens bound to one of its installations **and** legacy tokens with no installation, which are found through the operational customer id |
| `collector_installations` | Every installation is set to `retired` |
| `collector_enrollment_codes` | Every unused, unrevoked code is revoked |
| `ingest_key_ids` | Every active key is retired |
| `auth_sessions` | Revoked for users left with no usable organization (see below) |
| `tenant_lifecycle_events` | One `access_cutoff` row: who, when, counts, and the ids of the rows changed |

Nothing is deleted. Subscriptions are not touched: `cancelled` on the
organization is the authoritative service state, and billing is not coupled
to it. A future billing integration must reconcile subscription cancellation,
refunds and record retention explicitly.

If any step fails, the whole transaction rolls back: there is never a
cancelled organization with live tokens.

### what fails closed afterwards

| Path | Why it is refused |
|---|---|
| `/upload`, `/v2/upload`, heartbeats | Token inactive; organization not `active`/`trial`; bound installation `retired` |
| `/collector/enroll` | Code revoked; installation `retired`; organization `cancelled` |
| New enrollment code, new or edited installation | Refused by the service for a cancelled organization |
| New ingest key, new v2 cutover | Refused by the service for a cancelled organization |
| Dashboard | The organization disappears from every member's list and its access mode is `blocked`, checked on every rerun |
| Suspend / Reactivate | Refused: `cancelled` is terminal |

The Super Admin page also hides the corresponding controls, but the service
checks are what enforce this.

### sessions and users in several organizations

A session belongs to a user, and a user may belong to several organizations.

- A user with a membership in another organization that is not cancelled, or
  a platform admin, **keeps** their session. They can no longer see or open
  the cancelled organization.
- A user left with no usable organization has their persistent sessions
  revoked.

A suspended organization still counts as usable (it grants read-only access).

### repeating or reversing a cutoff

Offboarding is idempotent. Running it again revokes anything that became
live in the meantime, changes nothing already revoked, and appends another
`access_cutoff` row. Every invocation is recorded, and the rows tell the two
cases apart: the first cutoff has `status_before` of `active`, `trial` or
`suspended` and `repeat_sweep: false`; a repeat has `status_before:
cancelled`, `repeat_sweep: true`, and `changed: false` with every count zero
and every id list empty, unless something had become live again.

There is no un-offboard button. Until a purge has run, an accidental cutoff
can still be undone by a SortView operator by hand, because nothing was
deleted. The `access_cutoff` row lists the ids of exactly the tokens,
installations, codes and keys that call changed, so a reversal restores only
those and never switches on a token that had been revoked earlier for its
own reasons. A reversal must be recorded as an `access_cutoff_reverted`
lifecycle event. Sessions are not restored; users sign in again.

## data categories

Every application table has exactly one category.

| Category | Meaning | Tables |
|---|---|---|
| revoke | Access artifact. Disabled at cutoff, deleted by the purge | `organizations`, `agent_tokens`, `collector_installations`, `collector_enrollment_codes`, `ingest_key_ids` |
| purge | Tenant data. Untouched at cutoff, deleted by the purge | `checkins`, `rejects`, `acs_events`, `checkins_clean`, `rejects_clean`, `checkin_events`, `reject_events`, `acs_item_events`, `v2_cutovers`, `pipeline_status`, `branches`, `branch_settings`, `organization_settings`, `subscriptions`, `memberships`, `customers` |
| retain | Lifecycle evidence. Never deleted by a tenant purge | `tenant_lifecycle_events` |
| separately governed | Global user identity and security audit. Not deleted by a tenant purge | `app_users`, `auth_sessions`, `password_reset_tokens`, `auth_audit_log` |
| global / reference | Not tenant data | `plans`, `feature_entitlements`, `bin_routing_map`, `alembic_version` |
| provider aging | Held by a third party; ages out on its schedule | Neon history and branches, Sentry, hosting logs, GitHub Actions logs, e-mail provider |
| local machine | On the customer's Collector machine; removed only by the customer | Collector DataRoot, runtime backups, legacy token variable, legacy agent folder |

### users and the audit log are not purged with a tenant

A tenant purge removes the tenant's **memberships**. It does **not** delete
any person's global SortView account (`app_users`), their sessions or reset
tokens, or the global security-audit history (`auth_audit_log`), and it does
not anonymise them.

This is deliberate. Accounts are global and may have belonged to several
organizations, and the audit log has no tenant key. Deleting or anonymising
either needs a user-account lifecycle policy and an audit-retention policy
that do not exist yet. Until they do, a tenant purge must not be described
as deleting a person's account or their audit history.

### retention periods

No retention period is invented here. A purge is run when an authorized
operator runs it; nothing purges on a timer. Where a period already exists in
the product it is listed below, unchanged. A customer or legal retention
requirement, once known, is applied by choosing when to run the purge.

## the database purge

`scripts/purge_tenant_data.py`. Read its docstring before using it.

**Who and where.** An authorized SortView operator, connected as the table
owner, from a SortView operator machine. The tool opens a direct database
connection, which a city-owned machine and a library's Collector machine are
never permitted to do. It is not shipped in any Collector bundle and must
never be run on a customer's machine. It refuses to run as the application's
runtime role, `sortview_app`, which has no DELETE privilege on any table.

**The owner requirement is enforced, not assumed.** Before it looks up the
tenant, the tool requires the connected role to be the owner of every table
it uses: every table in the purge plan, `tenant_lifecycle_events` and
`alembic_version`. The operational tables are under row level security, and a
role that does not own them sees only what RLS allows, which without a tenant
context is nothing. Such a role's inventory, its unattributable-row check and
its "nothing left" check would all read zero while the rows were still there.
So a role that is not the owner is refused before any count is printed, and
is never told a purge would be accepted. Being granted DELETE does not
qualify a role, and neither superuser nor BYPASSRLS is accepted in place of
ownership. For the same reason the tool refuses if row level security has
been FORCED on any of those tables, because a forced policy filters the
owner's view too. None is forced today.

The tables whose ownership is checked are exactly the tables then read and
deleted from: those in schema `public`. The tool sets its own search path
for the transaction and verifies that every table name resolves to the
`public` table, so a search path configured on the role, the database or the
connection cannot redirect it to same-named tables in another schema.

**Step 1: inventory.** Without `--execute` the tool runs in a read-only
transaction, prints row counts per table, says whether a purge would be
accepted, and writes nothing.

```
DATABASE_URL=postgresql://... python scripts/purge_tenant_data.py \
    --organization-id <id> --confirm-slug <slug>
```

**Step 2: purge.** The slug a second time, and a named operator:

```
DATABASE_URL=postgresql://... python scripts/purge_tenant_data.py \
    --organization-id <id> --confirm-slug <slug> \
    --execute --confirm-purge <slug> --operator "<name>"
```

**It refuses, and writes nothing, unless all of these hold:**

- the database is PostgreSQL at this repository's Alembic head, and the
  connected role owns every table the tool uses (and is not `sortview_app`);
- the organization exists, the slug matches, and its status is `cancelled`;
- the access cutoff is complete: a recorded `access_cutoff` event, no active
  token, no installation that is not retired, no unrevoked unused code, no
  active ingest key;
- the operational mapping is unambiguous: the customer exists, exactly this
  one organization maps to it, and no branch has an operational id other
  than its own;
- no row pairs this tenant's customer with another tenant's branch, or the
  reverse;
- no row in `acs_events`, `checkins_clean` or `rejects_clean` has a NULL
  `customer_id` or `branch_id`.

**Unattributable rows.** `acs_events` holds the most sensitive legacy data
(`patron_id`, `raw_message`) and its tenant columns are nullable with no
foreign key. A row with a NULL key cannot be attributed to any tenant, so the
tool cannot prove it is not the tenant's. It reports the count and refuses.
Those rows must be classified or remediated by hand first. The tool never
infers an owner, and there is no documented rule for attributing such rows.

**What is deleted,** in this order, each statement scoped to the one tenant,
in one transaction:

1. `checkins_clean`, `rejects_clean`
2. `checkins`, `rejects`, `acs_events`
3. `checkin_events`, `reject_events`, `acs_item_events`
4. `ingest_key_ids`, `v2_cutovers`, `pipeline_status`
5. `agent_tokens`, `collector_enrollment_codes`, `collector_installations`
6. `branch_settings`, `branches`
7. `organization_settings`, `subscriptions`, `memberships`
8. `organizations`, then `customers`

If any tenant row survives, the transaction rolls back.

**What is kept.** `tenant_lifecycle_events` (a `purge_executed` row is added
in the same transaction, and a database trigger blocks UPDATE and DELETE on
that table for every role), the separately governed user and audit tables,
and the reference tables. `bin_routing_map` is one global table with no
tenant key and no patron or item data; it cannot be purged per tenant.

## what "deleted" means

After a successful purge the tool prints two separate statements. They are
different facts and must be recorded separately.

**LIVE DATABASE PURGE COMPLETE.** The tenant's rows no longer exist in the
live production database. The application, the dashboard and the API cannot
return them.

**PROVIDER HISTORY AGED OUT: NOT VERIFIED.** The purge does not, and cannot,
physically erase the data immediately:

- **Neon point-in-time history.** Deleted rows remain restorable until the
  project's history-retention window has passed since the purge. The window
  is a Neon project setting; production was verified at one day on
  2026-08-27 (`docs/backup-restore.md`). Read the current value from the
  Neon console at the time of the purge; do not assume it.
- **Neon branches.** Any branch created before the purge (a recovery drill, a
  `preserve_under_name` snapshot from a restore) holds the tenant's data
  until that branch is deleted by hand. List the project's branches after a
  purge and delete those that predate it.
- **Restoring afterwards brings the data back.** A point-in-time restore of
  production to a moment before the purge restores the tenant too. After
  such a restore, the offboarding and the purge must be run again.

"Provider history aged out" may be recorded only after the retention window
has passed **and** no branch created before the purge remains.

### current backup state

There is no off-platform backup. SortView relies on Neon point-in-time
restore alone. For offboarding this means there is no second copy to purge.
When a scheduled logical backup is added, it becomes another place a purged
tenant's data lives, and this document and the purge procedure must be
extended to cover its retention or deletion before it goes live.

### other providers

| Provider | What it may hold | Control |
|---|---|---|
| Sentry | API error events. Payloads are scrubbed before sending (`privacy_hardening`), so no patron, item or token values; internal ids may appear | Sentry's retention setting. Not documented in this repository: record the configured value when offboarding |
| DigitalOcean, Streamlit Community Cloud | Process logs: token ids, customer ids, reasons | The host's log retention |
| GitHub Actions | Monitoring-job logs | GitHub's log retention |
| E-mail provider | Password-reset messages to staff addresses | The provider's retention |

None of these can be purged from this repository.

## local Collector cleanup

Performed by the customer on their own machine. SortView cannot do it
remotely. After the access cutoff the installed Collector can no longer
upload, but its files remain until removed.

| Item | Location | Removed by |
|---|---|---|
| Application runtime | `<InstallRoot>` | `uninstall-collector.ps1` |
| Config, state, status, logs, run history | `<DataRoot>` | `uninstall-collector.ps1 -PurgeData` only |
| API token, v2 HMAC secret (DPAPI) | `<DataRoot>\secrets` | `-PurgeData` |
| v2 patron cache, hold ledger, quarantine | `<DataRoot>\data` | `-PurgeData` |
| `collector_config.json.bak-<timestamp>` copies | beside the config, under `<DataRoot>` | `-PurgeData` |
| Runtime backups from updates | `<InstallRoot>.backup-<timestamp>` and `...-full`, **outside** both roots | By hand. Code only, no data |
| Legacy token variable | Machine-scope `SORTVIEW_API_TOKEN` | By hand. The uninstaller reports it and prints the command |
| Legacy agent | `C:\SortViewAgent` and its task | By hand. Separate from the Collector |

- A plain uninstall **preserves** `<DataRoot>`. That is the default on
  purpose, so a reinstall keeps its state.
- `-PurgeData` asks for confirmation and removes `<DataRoot>` entirely,
  including the DPAPI secrets.
- While a Collector is still installed, its local files age out on their own
  schedules: `collector.log` is 5 MB with 3 backups, `runs.jsonl` keeps 30
  days, the v2 quarantine 90 days, the v2 patron cache 180 days and the hold
  ledger 90 days.

**Tech Logic source logs are vendor-owned.** SortView reads them; its
responsibility begins after reading. No SortView tool deletes them, and none
may be changed to.

## the runtime role cannot delete

The application connects as `sortview_app`. This was verified on 2026-10-01
for both production deployments separately: the API backend and the Super
Admin app, which is where offboarding is performed. That role holds no DELETE
and no TRUNCATE on any table.
Revocation is always an UPDATE. On `tenant_lifecycle_events` the role holds
INSERT, with USAGE on the table's id sequence, and nothing else: it can
record a cutoff but cannot read, change or remove one. The intended
privileges are recorded in `scripts/runtime_role_privileges.py`:

```
DATABASE_URL=postgresql://... python scripts/runtime_role_privileges.py verify
```

`verify` is read-only and exits non-zero if the role's effective privileges
differ from the baseline in either direction. Run it from an operator
machine after a migration and before and after an offboarding. It was added
because the role's grants were originally applied by hand and were not
recorded in the repository; `provisioning-sql` prints the statements for a
fresh environment, for the owner to review and apply. Neither command
changes a privilege.

## deployment order

Offboarding writes its evidence to `tenant_lifecycle_events`, which is
created by Alembic migration `a7c4e19d5b02`. Deploy in this order:

1. **Apply migration `a7c4e19d5b02`.** It is additive (one new table, one
   trigger, two grants) and safe to run against the previous application
   version.
2. **Verify the table, the trigger and the grants,** from a SortView operator
   machine:
   - `python scripts/runtime_role_privileges.py verify` reports OK. That
     confirms `sortview_app` holds INSERT on the table and USAGE on
     `tenant_lifecycle_events_id_seq`, and nothing else on either.
   - The append-only trigger exists:
     `SELECT tgname FROM pg_trigger WHERE tgrelid = 'public.tenant_lifecycle_events'::regclass AND NOT tgisinternal;`
     returns `trg_tenant_lifecycle_events_append_only`.
3. **Deploy the application code** that contains `offboard_library` and the
   Super Admin "Offboard Library (permanent)" control.

Migration first is the supported procedure. If the application were deployed
first, offboarding would fail when it tries to insert its evidence row into a
table that does not exist yet. That failure is closed: the whole cutoff runs
in one transaction, so it rolls back, the library is left exactly as it was,
and the page shows a fixed error. Nothing else in the application reads or
writes the table, so no other feature is affected by the order.

Rolling back: roll the application back before downgrading the migration.
The downgrade drops the table and every lifecycle record in it.

## verification checklist

After the access cutoff:

- [ ] Manage Libraries shows the library as **Cancelled**, with no Suspend,
      Reactivate, Add installation or Generate Enrollment Code control.
- [ ] Every installation shows `retired`.
- [ ] A Collector run on the customer's machine is rejected (403).
- [ ] A member with no other organization is signed out; a member of another
      organization no longer sees this one.
- [ ] One `access_cutoff` row exists in `tenant_lifecycle_events`.

Before the purge:

- [ ] The customer has confirmed, in writing, that the data may be deleted.
- [ ] The purge inventory has been run and its counts reviewed.
- [ ] The Neon history-retention window and the list of existing branches
      have been read from the Neon console and recorded.
- [ ] `runtime_role_privileges.py verify` reports OK.

After the purge:

- [ ] The tool printed `LIVE DATABASE PURGE COMPLETE`.
- [ ] A second inventory run reports that the organization does not exist.
- [ ] A `purge_executed` row exists in `tenant_lifecycle_events`.
- [ ] Neon branches created before the purge have been deleted, or listed
      with the date they will be.
- [ ] The customer has been told what remains on their Collector machine and
      how to remove it.
- [ ] After the retention window: provider history recorded as aged out.

## evidence to retain

Keep these with the offboarding record. None of them contains patron, item
or credential data.

- The customer's request or instruction, and who approved the purge.
- The purge inventory output and the purge's **EVIDENCE SUMMARY** output
  (operator, completion time, database, schema revision, rows deleted per
  table).
- The `tenant_lifecycle_events` rows for the organization. They remain in the
  database and are the system of record.
- The Neon retention window and branch list at the time of the purge, and
  the date provider history was recorded as aged out.
- Confirmation from the customer that the Collector was removed, and whether
  `-PurgeData` was used.

**`tenant_lifecycle_events` is append-only and protected, not tamper-proof.**
The application's database role can insert a row and nothing else: it cannot
read, update or delete one. A database trigger additionally rejects UPDATE
and DELETE for every role, the table owner included, so the purge tool and
an ordinary mistaken statement cannot remove or rewrite evidence. This is a
guard against accident and against the application, not cryptographic
immutability: the table owner can still drop the trigger or the table
(that is what the migration's downgrade does), rows are not signed or
hash-chained, and a point-in-time restore to before an event removes it. If
tamper-evidence is ever required, it needs a copy outside this database.

**What a `tenant_lifecycle_events` row retains:**

| Column | Content |
|---|---|
| `organization_id`, `organization_slug`, `operational_customer_id` | The tenant's identifiers, as plain values with no foreign key |
| `event_type` | `access_cutoff`, `access_cutoff_reverted` or `purge_executed` |
| `occurred_at` | When it happened |
| `actor_user_id`, `actor_label` | Who did it. For a cutoff, `actor_label` is the Super Admin operator's sign-in name, which is their staff e-mail address; for a purge it is the `--operator` value |
| `details` | Validated metadata: counts, internal row ids, status names, a schema revision |

This is platform and operator audit data, not patron or item data. It
survives the purge of the tenant it describes, and under the current policy
it is retained with no expiry, including the operator's e-mail address in
`actor_label`.

`details` must never contain a patron identifier, a barcode, a title, a token
or its hash, an enrollment code, or raw AMH content. The code that writes a
row validates this and rejects anything else; the only place a person is
named is `actor_label`.
