# C6a — Contract v2 Replacement-Machine Design

**Status:** Proposed — design review required  

**Scope:** Support-controlled replacement of an enrolled Contract v2 Collector computer, through Mode 1 controlled handoff or Mode 2 fresh-identity recovery  

**Implementation status:** Not implemented. Only the behavior listed in Section 2 and the code-inspection findings in Section 9 describe the system as it exists today; everything else here is proposed.

## 1. Objective

Allow a platform administrator to replace a retiring, failed, or lost Collector computer without losing stored branch analytics, creating competing active Collectors, or weakening Contract v2 security controls.

This workflow must support a new Collector installation on a clean Windows computer while preserving the existing customer, branch, historical event records, and original Contract v2 cutover.

### Replacement modes

C6a has two distinct replacement modes. The administrator selects one explicitly.

| | Mode 1 — controlled handoff | Mode 2 — fresh-identity recovery |
|---|---|---|
| **Used for** | Planned replacement of a live, uncompromised source that meets every requirement in Section 9A | A failed, lost, or compromised source, or any source that is ineligible for Mode 1 |
| **HMAC master secret** | Retained, through an encrypted handoff | New, generated locally |
| **`key_id`** | Retained; exclusive ownership moves to the replacement installation | New; the old key is retired |
| **Source position** | Validated cursors, rebound to the migrated source files | Bootstrap at end-of-file |
| **Local state** | Transferred (see Section 9A) | None transferred |
| **Reporting** | Intended to continue without a boundary, subject to validation | Explicit reporting discontinuity (see Section 9B) |

A Mode 1 failure must never fall back to Mode 2 automatically. Mode 2 changes the branch's identity and reporting continuity, so it requires its own explicit administrator decision.

## 2. Existing behavior

- Collector 1.0.14 supports guided Contract v2 enrollment on a clean machine.
- Installations support Provisioning, Active, Inactive, and Retired states.
- Agent tokens are associated with installations through `agent_tokens.installation_id`.
- Ingest keys are currently associated with a customer and branch, not a specific installation.
- Ordinary Contract v2 enrollment is blocked when a branch has an active ingest key or any previous v2 cutover.
- Both enrollment-code issuance and enrollment redemption enforce these restrictions.
- The server stores no Collector HMAC secret; the secret is generated and protected locally.
- Fresh Collector bootstrap starts at the end of each source file.
- `event_key` and `item_key` derive from the master secret, not from `key_id`. Server deduplication is nevertheless scoped to `key_id`: `/v2/upload` uses `ON CONFLICT (customer_id, branch_id, key_id, event_key) DO NOTHING`.
- An installation in `inactive` or `retired` status stops its bound agent token at authentication, for uploads and heartbeats alike. The check takes no lock. It does not stop a legacy unbound token, and it does not change the ingest key. `inactive` can return to `provisioning`, and the next accepted heartbeat makes the installation `active`.
- `/v2/upload` and `/v2/status` check that the ingest key is active and belongs to the token's tenant. They do not check which installation owns the key.
- The holds report reads `acs_item_events` without a `key_id` filter and selects each item's latest record by `(event_time, id)`. `id` is a table-wide `BIGSERIAL`, so it orders rows across keys by insertion.
- Check-in and reject reports count stored rows with no `key_id` filter or `DISTINCT`. The application runtime role cannot update or delete event rows.
- A v2 cursor is an offset plus a `(st_dev, st_ino)` file identity. A missing cursor or an identity mismatch makes the reader start that file at offset 0.

The following do not exist today: a Collector drain command, handoff export or import, secret import, cursor rebinding, installation ownership of ingest keys, or a reporting discontinuity display.

These existing safeguards must remain intact for ordinary enrollment.

## 3. Proposed replacement identity

Each replacement computer receives a new `collector_installations` record.

The replacement installation retains the original operational customer and branch associations but has its own installation ID, one-time authorization code, and agent token.

- **Mode 1:** The replacement retains the source's HMAC master secret and `key_id`, so one secret stays bound to one `key_id`. Exclusive ownership of the key moves to the replacement at activation.
- **Mode 2:** The replacement receives a new `key_id` and generates its own HMAC secret locally.

The master secret is never sent to or stored on the SortView server in either mode.

Historical installation records remain available for auditing.

The original branch cutover must not be deleted, overwritten, or recreated.

## 4. Proposed database changes

Add a nullable `installation_id` foreign key to `ingest_key_ids`, referencing `collector_installations.id`.

Requirements:

- New Contract v2 enrollment keys are explicitly associated with their installation.
- Existing keys with `NULL` installation IDs remain valid under existing behavior.
- Replacement must refuse ambiguous ownership; it must not infer an installation from branch identity alone.
- Database constraints and indexes must support installation-scoped ownership verification.
- PostgreSQL runtime permissions and row-level security behavior must be reviewed and tested before deployment.
- Migration and deployment must preserve existing events and keys.

The exact uniqueness constraint and policy for legacy keys require implementation review.

### Additional ownership and compatibility requirements

Token-to-ingest-key installation ownership is enforced when `ingest_key_ids.installation_id` is non-NULL. Existing NULL-owner keys remain subject to their established tenant and key-status checks until ownership is safely established.

A key must not be backfilled with an installation owner while active unbound tokens could be unintentionally locked out. Existing tokens, ownership evidence, and operational dependencies must be reviewed before backfilling.

An ambiguous ownership relationship must not be inferred from customer, branch, creation order, or latest heartbeat alone. Such cases require explicit operator reconciliation before replacement.

### Mode 1 ownership requirements

Mode 1 requires a key with an explicit, non-NULL owner equal to the source installation. A NULL-owner key must be reconciled first.

Activation moves ownership to the replacement in the same transaction that invalidates the source's tokens. The key stays active, its stored events are not rewritten, and at no point does more than one installation own it.

Ownership enforcement must reject every token not bound to the owning installation, including any active unbound token for the branch. This is required because today's key check authorizes by tenant only, so a second token at the branch could otherwise upload under the retained `key_id`.

Event rows record `key_id` but not an installation, so under a retained key the rows do not show which computer sent them. Attribution across a handoff relies on the audit trail and `received_at`. `received_at` defaults to the inserting transaction's start time, so it is not proof of when a row committed relative to the upload fence.

## 5. Replacement authorization

Replacement is an explicit platform-administrator operation, separate from ordinary installation creation and enrollment.

The administrator must identify the existing installation, select Mode 1 or Mode 2, and confirm replacement. Mode 1 additionally requires an explicit attestation that the source is not suspected of compromise.

The server must verify:

- The library and branch are operational and eligible.
- The selected installation belongs to the specified library and branch.
- The installation uses Contract v2.
- The ingest key being retired belongs to the expected customer, branch, and installation.
- The requested replacement does not conflict with another active or pending replacement.
- The authorization cannot be reused for another installation or branch.
- For Mode 1, every eligibility requirement in Section 9A holds.

An ordinary enrollment code must never act as replacement authorization by itself.

## 6. Server-side replacement workflow

Authorization and credential cutover are separate phases in both modes. Mode 1 adds the source preparation and upload fence of Section 9A between them. Phase B's "redemption" is the operation Section 9A calls "activation" in Mode 1.

****Phase A — Authorization****

A platform administrator authorizes replacement of an existing Contract v2 installation.

The server creates a new Provisioning installation explicitly linked to the installation it replaces. It records the authorization and permits a short-lived, one-time replacement enrollment code.

The original installation, agent tokens, and ingest key remain operational during this phase. In Mode 1 the source stops uploading later, at the upload fence, while its credentials stay unrevoked until activation.

Authorization must reject concurrent pending replacements, invalid tenant relationships, and ambiguous key ownership. A compromised old machine must be deactivated immediately through an explicit security response rather than left operating until redemption, and it may only be replaced through Mode 2. Deactivation does not stop a legacy unbound token, so any such token must be revoked separately.

An abandoned replacement may be cancelled without interrupting the original Collector, unless a Mode 1 source has already been prepared or fenced. Section 10 defines that case.

****Phase B — Enrollment redemption and credential cutover****

Successful redemption must atomically:

1. Validate the enrollment code and replacement authorization.
2. Serialize the operation against competing branch enrollment or replacement requests.
3. Verify the original installation, ingest-key ownership, effective v2 cutover, and applicable token restrictions.
4. Transition a Provisioning, Active, or Inactive source installation to Retired; leave an already Retired source unchanged after verifying its eligibility and ownership.
5. Invalidate the old installation's agent tokens.
6. **Mode 2:** Retire the verified old ingest key. **Mode 1:** Keep the key active and move its exclusive ownership to the replacement installation.
7. Issue a new installation-bound agent token. **Mode 2:** Also issue a new ingest key. **Mode 1:** Issue no new key.
8. Mark the one-time code as consumed and, in Mode 1, mark the handoff as activated.
9. Preserve the original v2 cutover and historical events.
10. Record the credential transition in the audit trail.

Any failed database operation must roll back the complete cutover.

Both `/v2/upload` and `/v2/status` must enforce token-to-ingest-key installation ownership. Active unbound tokens must be resolved explicitly before replacement; they cannot silently bypass installation isolation.

Replacement-specific enrollment refusals must remain compatible with the existing Collector 1.0.14 installer.

### Additional redemption and recovery rules

The normal replacement cutover retires the old installation and invalidates its bound credentials.

A Provisioning, Active, or Inactive source transitions to Retired. An already Retired source remains unchanged. All sources require identity, historical key ownership, and replacement-eligibility verification.

Mode 1 is refused when the branch has no active ingest key. In that case, Mode 2 replacement may proceed only through an explicitly authorized recovery path that verifies the source installation's ownership of its most recent key and confirms that no competing active key or Collector exists. Otherwise the operation must fail closed and require operator reconciliation.

The replacement transaction must verify the result of each required key-retirement operation. An unexpected zero-row update is a failure, not a successful revocation.

Any token or key ownership migration must preserve the behavior of existing NULL-owner keys until reconciliation is complete.

### Source installation lifecycle clarification

During successful replacement redemption, a Provisioning, Active, or Inactive source installation transitions to Retired.

An already Retired source remains Retired. Its identity, historical key ownership, and replacement eligibility must still be verified.

No replacement operation may leave the superseded source installation eligible to return to Provisioning.

## 7. Replacement enrollment

The new machine must use the existing guided installer where practical.

The enrollment service must distinguish authorized replacement enrollment from ordinary enrollment and enforce the distinction independently of the administration interface.

For an authorized Mode 2 replacement, redemption should:

- Validate the replacement authorization and its installation binding.
- Preserve the branch's original v2 cutover.
- Issue a new agent token and ingest key.
- Generate no server-side HMAC secret.
- Return the existing Contract v2 configuration and classification rules.
- Ensure replacement authorization cannot be redeemed multiple times.

A Mode 2 replacement machine generates and protects its own HMAC secret.

### Mode 1 activation and installer compatibility

Mode 1 needs a dedicated handoff path. These requirements follow from how the current Collector and installer behave:

- **Version gate:** Both machines must run a handoff-compatible Collector. Collector 1.0.14 and earlier have no drain, export, or import capability, so an eligible source must first be upgraded through the existing release-update process. No handoff-compatible version exists yet.
- **Refusing older destinations:** A destination too old for Mode 1 must receive the existing `collector_upgrade_required` refusal, because the installer accepts only a closed set of refusal codes. The refusal must not consume the code.
- **Response shape:** Activation must not add fields to the existing enrollment response, because the installer's parser rejects unexpected fields. A separate route or an explicitly request-gated response is required.
- **Clean-machine check:** `Get-LeftoverArtifacts` correctly refuses a pre-existing secret, cursor, or rules file during ordinary setup, and it must keep doing so. Mode 1 needs its own explicit path. `patron_cache.db` and `quarantine_v2.json` are not in that check today; import must refuse to overwrite them, and the check should add them.
- **Bootstrap:** `bootstrap_v2_state` must not run during Mode 1, because it seeds cursors at end-of-file. `finish-collector-install.ps1` already skips bootstrap when a valid v2 state file exists, but that path must be verified for Mode 1 rather than assumed to be sufficient.
- **Secret import:** `SecretStore` can only generate a secret today. Import is new functionality. It must refuse to overwrite a secret, seal the secret with machine-scope DPAPI on the destination, bind it to the retained `key_id`, and verify the protected folder ACL. A DPAPI blob is never copied between machines.
- **Scheduled Task:** The destination's task must not be registered or run until import is complete, because a missing v2 cursor makes the reader start at offset 0.
- **Commands:** New Collector commands must be added to the frozen build's dispatcher allowlist.

## 8. Concurrency and security

The workflow must prevent:

- Two simultaneous replacements for one branch.
- Multiple valid Collectors operating under competing installations.
- Reuse of the old installation's revoked credentials.
- Reuse of retired ingest keys.
- Cross-library or cross-branch replacement.
- Unauthorized enrollment through ordinary provisioning routes.
- Partial database changes following an operation failure.
- A Mode 1 source running or uploading after its final drain.
- Activation from a replaced, expired, cancelled, or already-consumed Mode 1 package.
- Upload under a retained `key_id` by any token not bound to the owning installation.
- Use of Mode 1 where compromise is suspected.

Branch-scoped locking and transactional checks must cover both replacement authorization and enrollment redemption.

The final implementation must define how abandoned replacement authorizations are cancelled or resumed.

## 9. Cursor, reporting continuity, and historical integrity

A Mode 2 replacement machine creates a new machine-local HMAC secret and therefore a new event and item identity namespace. Section 9A defines how Mode 1 avoids this, and Section 9B defines Mode 2's approved behavior.

A fresh bootstrap at the end of the source files can create permanent ingestion gaps, including events generated during an outage or between credential cutover and bootstrap.

The changed HMAC identity namespace may also prevent item-level matching across the replacement boundary. Existing hold de-duplication, hold retractions, and classification corrections require investigation.

****These risks are not approved for general commercial release.****

Before C6a implementation is approved, determine:

1. Which source events can be lost during planned and unplanned replacement.
2. Whether a bounded, privacy-preserving replay or handoff mechanism is feasible.
3. How event duplication is prevented. Deduplication is scoped to `key_id`, so a new `key_id` would duplicate replayed events even with an unchanged secret, and events under different HMAC identities cannot be matched at all.
4. How historical holds and later retractions behave across a key transition.
5. Whether the dashboard and reports must identify a replacement boundary, ingestion gap, or reduced-confidence reporting period.
6. How replacement monitoring detects an installation that redeems its code but never begins successful ingestion.

If lossless continuity cannot be guaranteed, the limitations and a tested mitigation must be explicitly reviewed before commercialization.

Do not treat an undocumented permanent reporting gap as acceptable behavior.

**Disposition:** The October 31, 2026 decisions in Section 13 resolve items 2–4 at the architecture level: Mode 1 uses a validated handoff under a retained identity, and Mode 2 performs no automatic replay. Items 1, 5, and 6 still require implementation evidence and validation.

### Confirmed continuity findings — October 31, 2026

Code inspection established the following. These findings describe Collector 1.0.14 and a replacement that generates a new secret, which is now Mode 2. Findings 2, 3, 5, and 6 do not apply to a Mode 1 replacement that retains the secret and receives the source's local state.

1. **Fresh bootstrap starts at EOF.** `collector/bootstrap_state.py::bootstrap_v2_state` initializes source cursors from `reader.read_new_lines(path, None)` and refuses existing v2 or legacy state. Collector 1.0.14 has no supported cursor-handoff or bounded-replay initialization mode.

2. **Item identities change with the master secret.** `collector/v2_identity.py` derives `K_item` from the machine-local master secret. A replacement generating a new secret produces different `item_key` values for the same barcode.

3. **Cross-key hold retractions cannot match.** `src/metrics_v2.py::build_acs_item_summary_v2` keeps the latest record per `item_key`. A hold under the old identity and a later `non_hold_101` record under the new identity are treated as separate items.

4. **Non-hold events do not require an existing local hold.** `collector/v2_transform.py` emits non-hold ACS events even when `cache.hold_get(item_key)` returns no row. The missing retraction is therefore a cloud identity-correlation issue, not simply an omitted event.

5. **Historical classification corrections require local state.** `_corrections()` uses the patron cache and hold ledger to generate updated hold events. A replacement with an empty ledger cannot generate corrections for holds known only to the previous installation.

6. **Copying local SQLite state is insufficient.** `collector/v2_patrons.py` stores HMAC-derived patron and item identifiers. Those identifiers do not match a replacement's newly derived subkeys.

7. **The existing master secret is machine- and key-bound.** `collector/v2_keys.py` uses Windows DPAPI machine-scope protection and refuses loading a secret whose stored `key_id` differs from the configured key. There is no supported cross-machine secret recovery or transfer.

**Conclusion:** Cursor-only migration cannot provide complete reporting continuity. The approved C6a implementation design must address event recovery, cross-key item correlation, and local classification state together.

These are findings from code inspection, not executed replacement-specific tests. Sections 9A and 9B are the approved architectural response. They have not been implemented or tested.

A new Collector release is required for Mode 1. Its exact scope remains undetermined until the implementation design is complete.

### Additional ingestion-continuity findings

Collector v2 uses the Tech Logic source files as its durable source of undelivered records. It advances its read cursors only after successful delivery. There is no separate persistent upload outbox.

Therefore, undelivered records may remain available beyond the previous Collector's committed cursors.

Disposition of the original investigation items:

- Planned replacement using a verified handoff of the old Collector's committed cursors: adopted for Mode 1 (Section 9A).
- Failed-machine recovery using a bounded replay start derived from available server-side event history: not adopted. Mode 2 performs no automatic replay across identities.
- Timestamp ties and other ambiguities at a proposed replay boundary: no replay boundary exists in the approved design. Report tie ordering is covered in Section 9A.
- File rotation, truncation, replacement, and source-file identity changes: still open, and a real-machine validation gate for Mode 1.
- Source files or cursor state lost with the original computer: Mode 1 is unavailable, and Mode 2 applies.
- Duplicate detection across different HMAC identity namespaces: not available, and not needed without cross-identity replay.

**Collector and installer compatibility:** Collector 1.0.14 supports the existing fresh-enrollment response, but its clean-machine check refuses prior state and its bootstrap refuses an existing state file and seeds only at end-of-file. A cursor handoff or bounded replay may require a new Collector release and installer/bootstrap changes. Evaluate these costs and the compatibility of deployed 1.0.14 Collectors before selecting a continuity mitigation. The requirement to preserve 1.0.14 enrollment refusal behavior does not imply continuity can be delivered server-only.

**Local classification-state continuity:** Investigate the local patron cache, hold ledger, and HMAC-derived patron and item identifiers. Cursor transfer alone does not transfer those states. A replacement machine with a new secret may not be able to use copied local cache data; historical holds, classifications, and later corrections or retractions may behave differently. Assess privacy-safe transfer or reconstruction options and prove reporting correctness across the key boundary rather than assuming cursor handoff solves hold continuity. In the approved design, Mode 1 transfers this state under the retained secret, and Mode 2 does not transfer it.

The Section 9A cursor handoff is approved as a design only. It must not be implemented for production use or relied on until the gates below are met. No replay method is approved.

### Approval gates

****Before C6a implementation approval:****

The investigation must establish the actual ingestion-gap and cross-key hold-integrity failure modes, identify feasible mitigation approaches, and produce an approved implementation design.

This includes investigating replay and cursor handoff, historical hold/retraction behavior, reporting-gap visibility, and monitoring of incomplete replacements.

****Before commercial release:****

The selected mitigation must be implemented and tested. Remaining limitations must be explicitly documented and approved; permanent ingestion gaps or hold-count inconsistencies cannot be silently accepted.

****Before production release of Mode 1:****

- Real PostgreSQL regression tests must prove same-item and cross-key timestamp ties through the actual holds report. Such tests do not exist today.
- Source-file continuity, cursor rebinding, and Tech Logic file behavior must be validated on a real machine. `collector/reader.py` states that its file-identity and rotation handling has not been proven against real Tech Logic behavior.
- `cryptography` must pass dependency, security, and frozen-build validation, or an alternative must be separately approved.
- The Section 10 failure stages must be exercised by crash-injection testing.

## 9A. Mode 1 — controlled handoff

This section is an approved design, not existing functionality. Where a mechanism is not yet decided, this section states the required invariant and lists the mechanism as an open engineering item.

### Eligibility

Mode 1 is permitted only when all of the following hold. If any does not, Mode 1 is refused.

- The replacement is planned, and the source computer is running.
- The source is not suspected of compromise, and an administrator has attested to this.
- The source installation is Active and owns the branch's active ingest key.
- Every token that can use the key is bound to the source installation. Legacy unbound tokens must be resolved first.
- Both computers run a handoff-compatible Collector version.
- The source can complete the final drain below.
- Tech Logic source files can move to the replacement with validated continuity.
- The destination is a clean machine with no existing Collector state.

A failed, lost, compromised, Inactive, or Retired source cannot use Mode 1. An Inactive source's token is refused by the server, so it cannot drain. An Inactive source that is not suspected of compromise may be deliberately returned to Active first and then assessed.

### Identity and deduplication

The source's master secret and `key_id` remain one identity. Because deduplication is scoped to `(customer_id, branch_id, key_id, event_key)`, keeping the `key_id` means a resent event with an unchanged `event_key` is stored once rather than duplicated.

This protection is a safety net, not permission to replay. An unchanged `event_key` also depends on the same rules, time zone, parser and normalizer behavior, canonical version, patron guard state, and ledger revision. Source bytes that were never uploaded, such as lines before the original bootstrap, would be new events. Stored check-in and reject rows are counted directly and cannot be removed by the application, so a bad replay would permanently inflate reports.

### Transferred-state inventory

| State | Treatment |
|---|---|
| HMAC master secret and its `key_id` binding | Transferred inside the package; re-sealed with DPAPI on the destination |
| `patron_cache.db` (patron records, hold ledger, ruleset registry) | Transferred as a consistent snapshot |
| `quarantine_v2.json` | Transferred, so quarantined events are not re-sent |
| `state_v2.json` cursors | Offsets transferred; file identities rebound on the destination |
| Classification rules file | The source's exact file transferred |
| Time zone | The source's exact value transferred |
| Custom `v2` limits and other identity-relevant configuration | Transferred; machine-specific paths are re-established and verified |
| Source-file verification values | Created at export; see "Source-file verification" below |
| Source build and canonical version | Recorded and checked for compatibility |
| Agent token | **Not transferred:** the replacement receives its own |
| DPAPI blob | **Not transferred:** it is machine-bound |
| `status_v2.json` and run logs | **Not transferred:** local reporting only |
| Raw source lines | **Never in the package:** the Tech Logic files migrate separately |

The rules and time zone are preserved exactly, even if the server's current settings differ. Any later change requires a separate validated workflow, because changing them changes future event identities and classifications.

### Handoff sequence

The order is mandatory. A step does not begin until the previous one has been confirmed.

1. **Authorize.** An administrator authorizes Mode 1 (Section 5). The server records a handoff with a unique ID, source and destination installations, `key_id`, status, and expiry.
2. **Prepare the destination.** The destination generates an ephemeral key pair, protects the private key locally, and uses the one-time code to register its public key fingerprint against the handoff.
3. **Stop new source data.** Tech Logic writes to the source files stop. Sorter downtime starts here and lasts through step 11.
4. **Lock the source.** The Scheduled Task is disabled, an exclusive export lock is taken, and no Collector run may be active. `MultipleInstancesPolicy=IgnoreNew` only governs task launches, so it is not sufficient. This lock does not exist today.
5. **Final drain.** While the source's credentials are still usable, the Collector processes every configured source until all of the following are true:
   - Every complete line has been read.
   - Every event was delivered or quarantined under existing policy.
   - The cache and cursors are committed.
   - No delivery failed, and no chunk budget stopped processing early.

   A missing source or a failed delivery blocks export. A partial trailing line stays unread and is carried in the migrated file.
6. **Synchronize, then snapshot.** After the final drain, the server establishes the synchronization point defined under "In-flight request synchronization". Only then, while the lock is held, does the source capture the inventory above from its committed state. SQLite must use a consistent backup mechanism such as `VACUUM INTO`, followed by an integrity check. A plain copy of the database file is prohibited.
7. **Build the package and block local runs.** The source verifies through the server that the destination key is the one registered for this handoff, encrypts the package to it, and writes a local tombstone that refuses later Collector runs.
8. **Upload fence.** In one authenticated call, the source registers the package digest and moves its own installation to `inactive`. The call must not succeed partially, and it must be refused if the cutover guarantee below no longer holds. Its token stops working once it succeeds, so a lost response is resolved by an administrator reading the server state.
9. **Migrate and validate.** The technician carries the package and migrates the Tech Logic files. The destination completes all validation below before activation.
10. **Activate.** The server applies Phase B (Section 6) as one transaction and returns the new token and configuration.
11. **Install.** The destination seals the secret to the retained `key_id`, then installs the cache, quarantine, rules, time zone, and limits. It writes the rebound `state_v2.json` **last**, as the completion marker. Only then is the Scheduled Task registered and Tech Logic allowed to write again.
12. **Confirm and retire.** After `v2-key check`, preflight, and an accepted heartbeat, the destination deletes the package and private key, and the source's operational secrets are removed.

### In-flight request synchronization

The server status check runs at authentication and takes no lock, so a request authenticated just before the fence can still commit after it.

**Required invariant:** the final drain, server-side synchronization, state snapshot, and upload fence establish one consistent cutover. No upload authenticated earlier may commit later in a way that leaves the exported cursor or ledger stale.

**Required ordering guarantee:**

1. **Final drain completes.** The source holds its lock and has its delivery results.
2. **Synchronization point.** Every upload under the source's token or `key_id` that was authenticated before this point has either committed or can no longer commit. Later uploads under that token or key must not change the cutover state.
3. **Snapshot.** The cursor and ledger are captured only after the synchronization point, so they reflect the completed drain with no upload still pending.
4. **Upload fence.** The fence commits only if no upload under the source's token or `key_id` has committed since the synchronization point. Otherwise the fence is refused, the package is discarded, and a new drain and snapshot are required.

After the fence commits, no upload under the source's token can commit. The same must hold between fence, activation, and cancellation.

A failure before the fence commits must leave the source able to resume uploading once the attempt is abandoned.

How the server establishes the synchronization point and enforces this ordering is an open engineering item and must be reviewed for its cost on the upload path. The local lock and tombstone are required as well, but they do not replace this server-side invariant.

`received_at` and `id` cannot prove this after the fact: `received_at` reflects transaction start, and `id` values are allocated before commit.

### Package requirements

The package must be:

- **Encrypted and authenticated** with authenticated encryption, using a key agreement with the destination's ephemeral key. Python `cryptography` is preferred, subject to validation. The Collector has no authenticated-encryption dependency today, and the exact construction is an open engineering item.
- **Destination-bound:** only the registered destination can open it, and the source encrypts only to a key the server authorized for this handoff.
- **Context-bound:** the handoff ID, customer, branch, source and destination installations, `key_id`, format version, and expiry are authenticated with the content.
- **Expiring:** the server's time is authoritative for activation. Expiry never lifts the upload fence by itself.
- **Single-use:** one digest per handoff, and one successful activation.
- **Technician-carried:** the server stores the digest and handoff metadata, never the ciphertext, the master secret, or any key that could open the package.

The package and any temporary or staged files are kept only in folders protected with verified ACLs. No secret, cache content, or package key may be written to logs or audit records.

### Destination authentication and validation

Activation must prove all of the following: possession of the one-time code, possession of the private key matching the registered fingerprint, and a package digest matching the one the source registered. The exact exchange is an open engineering item.

Before activation, the destination must verify:

- The package authenticates, opens, and matches this handoff.
- The handoff is unexpired and not cancelled or consumed.
- The SQLite snapshot passes its integrity check.
- The master secret, rules, and cache belong together, by recomputing the keyed ruleset fingerprint and matching it against the cache's `rulesets` row.
- The Collector build is compatible with the recorded canonical version.
- Every source file passes the verification below.

Any failure stops the handoff before activation, with nothing consumed.

### Source-file verification and cursor rebinding

A cursor's file identity is machine-specific, so a copied cursor would fail its identity check and restart the file at offset 0. That must never happen silently during a handoff.

**Required invariants:**

- Three values are distinct for each file:
  - **Saved cursor offset:** the position up to which the source has consumed the file.
  - **Authenticated prefix length:** the number of bytes covered by the keyed digest. It must equal the saved cursor offset, so the digest covers every previously consumed byte.
  - **Source file size:** the file's size at export. It may exceed the saved cursor offset, for example by a partial trailing line. It is recorded for diagnostics and is not the verified length.
- At export, the source records these values and a keyed digest of the authenticated prefix, under a key derived for this handoff. A bare hash is not acceptable, because the content can be guessable. No raw line enters the package.
- On the destination, each file must be at least as long as the authenticated prefix, and its first bytes up to that length must match the digest. Additional bytes after the prefix are legitimate unprocessed data and do not fail verification.
- Only after that check may the saved cursor offset be rebound, unchanged, to the destination file's new identity. It must never be moved to the destination file's size.
- A missing file, or a missing or mismatched consumed prefix, fails closed: no cursor is written, the Collector does not run, and support must investigate.
- Bytes after the saved cursor offset are read for the first time by the destination.

How Tech Logic and its files move is an open operational item. Whether a migrated file behaves correctly under real Tech Logic rotation and truncation requires real-machine validation. The handoff does not change the reader's ordinary rotation behavior, which restarts a file at offset 0, so post-handoff behavior must be validated too.

### Stale state, replay, and concurrent operation

- **Stale ledger:** a stale ledger can regenerate a correction that already exists on the server. Deduplication then drops it, and the wrong record stays latest. Only the package created from the final drain may ever activate, and restoring a cache from any other copy is prohibited.
- **Replaced packages:** cancelling invalidates the handoff's package. Any new attempt requires a new handoff, drain, snapshot, and package.
- **Package replay:** a used, expired, cancelled, or substituted package is refused.
- **Concurrent operation:** the tombstone, upload fence, token invalidation, and ownership enforcement independently stop the source. The destination does not run until its cursors are installed.
- **Branch serialization:** the fence and activation must take part in the branch-scoped locking in Section 8.

### Source secret retirement

After activation is confirmed, the source's master secret, cache, quarantine, cursors, rules, and agent token are removed, and the technician records an attestation in the audit trail.

This is operational removal, not guaranteed physical erasure. Remnants may survive in free disk space, shadow copies, or backups. Until removal, the master secret exists on two computers, and the retired source's credentials do not remove that exposure. If removal cannot be confirmed, the attestation stays open and the risk stays recorded. If the source or its data is later suspected of compromise, the branch requires a Mode 2 identity replacement.

### Security policy

The master secret may leave its computer only through an authorized, encrypted Mode 1 handoff. There is no plaintext export, and no secret is ever sent to or stored on the SortView server. This amends the "no recovery" statements in `collector/v2_keys.py` and `collector/v2_identity.py`, which must be updated when Mode 1 is implemented. `docs/contract-v2-design.md` section 5 must also be corrected, because it says rotating `key_id` yields new event keys, when the secret is what determines them.

### Contract v2 report compatibility

Mode 1 requires no report change. The holds report ignores `key_id` and selects each item's greatest `(event_time, id)`, and corrections and same-second retractions depend on that ordering. Under a retained secret and `key_id`, later corrections and retractions apply to the same `item_key` as earlier holds, and later rows receive higher `id` values.

The handoff must preserve that behavior by carrying the ledger's revisions forward and must not rewrite stored rows or `id` values. This depends on insertion order following the Collector's send order, which the real PostgreSQL tests must confirm.

## 9B. Mode 2 — fresh-identity recovery

Mode 2 applies when Mode 1 is ineligible, including every failed, lost, or compromised source. It is an approved design and is not implemented.

The replacement receives a new installation, agent token, ingest key, and locally generated secret, then bootstraps at end-of-file. Server history is preserved and not rewritten. Historical data is never replayed automatically across identities, because events under different secrets cannot be deduplicated.

Mode 2 has these known limitations:

- Records written after the old Collector's last committed cursor and before the new bootstrap may be permanently missing from reports.
- An item's earlier holds cannot be matched to later records under the new identity, so a hold from before the boundary cannot be retracted or corrected by a later record.
- A range spanning the boundary can count the same physical item once under each identity.
- Classification corrections are unavailable for holds known only to the old installation.

Reports must display an explicit discontinuity for any affected range. No such display exists today. Its data source, its presentation, and any impact on API responses are open engineering items.

## 10. Failure recovery

Replacement authorization and enrollment redemption are distinct phases.

****Failures before redemption:****

- The existing Collector and credentials remain operational. In Mode 1 this holds only until source preparation begins; the table below covers the later stages.
- An abandoned replacement can be cancelled.
- An expired enrollment code can be reissued under the same valid pending replacement authorization.
- A compromised source machine is handled through immediate deactivation and Mode 2, rather than normal planned-replacement continuity. A legacy unbound token must be revoked separately.

****Failures after successful redemption:****

- The old installation remains Retired, and its previous credentials remain invalid.
- The replacement installer may resume when its local setup state is intact.
- In Mode 2, an enrollment response lost after server-side success may leave the replacement computer without the token, key ID, classification rules, and other enrollment configuration. An installation-bound recovery token alone cannot repair that state. The proposed recovery is another explicitly authorized replacement from the unconfirmed installation, subject to validation of the replacement chain, ownership, one-time-code rules, and branch key restrictions. Do not retry a consumed code as if it were unused.
- A failed replacement machine may itself require another explicitly authorized replacement.
- Recovery must not recreate the original cutover, reactivate old credentials, or issue competing active ingest keys.

All recovery actions must be auditable.

### Mode 1 failure stages and rollback boundaries

The commit of activation is the rollback boundary. Before it, cancellation can restore the source. After it, recovery only moves forward.

| Stage | Failure | Required outcome |
|---|---|---|
| Authorized; source not yet prepared | Abandoned or expired | Cancel or reissue. The source is unaffected. |
| Lock, drain, snapshot, or package build | Any failure | No fence and no usable package. Discard partial output, release the lock, and let the source resume. Events already delivered remain stored, and re-delivering them is deduplicated under the same key. |
| Upload fence | Response lost | An administrator reads the server state. If not fenced, retry or cancel. If fenced, continue. |
| After fence, before activation | Validation failure, lost package, expiry, or abandonment | The source stays fenced until an administrator cancels. Cancellation invalidates the handoff and package, returns the source from `inactive` to `provisioning`, and only then may the tombstone be removed and the task restored. Its next accepted heartbeat makes it `active`. A new attempt starts a new handoff. |
| After fence; source and package both lost | Source computer failed | Cancel the handoff and use Mode 2. |
| Activation transaction | Any failure | The entire transaction rolls back. The source stays fenced. Retry or cancel. |
| Activation committed; response lost | Destination has no token or configuration | Recovery runs through the original handoff, with the same destination proof, and must not activate a second time. The server stores only token hashes, so recovery replaces the installation's unconfirmed token rather than replaying the original. The exact mechanism and when recovery closes are open engineering items. |
| After activation; before the cursor marker | Crash during install | Resume on the same destination from the retained package and re-verify. Bootstrap must not run. |
| After install | Destination later fails | No rollback, and the source stays Retired. A healthy destination may later be the source of a new Mode 1. Otherwise use Mode 2. |
| Source secret retirement | Source unreachable or removal fails | Activation stands. The attestation stays open, and the residual risk is recorded. |

No recovery path may reactivate source credentials after activation, create a second owner of the key, or create a second activation.

### Recovery after local token-storage failure

If replacement enrollment succeeds on the server but the installer cannot securely store the returned agent token, installation must stop without creating another ingest identity.

SortView support must be able to issue a recovery token explicitly bound to the replacement installation. The recovered token must pass the same installation-to-ingest-key ownership checks as the original token.

The existing installer resume and token-storage process should be reused where possible.

A replacement must not be repeated solely because the installer failed to store a token, provided the installation can be securely recovered.

The required installation-bound recovery-token capability is part of C6a implementation scope; the existing unbound token-generation script is insufficient.

**Recovery-token issuance requirements:** The operation must be accessible only to an authorized platform administrator or support role and must produce a durable audit record. It is permitted only for a Provisioning or Active replacement installation that owns the applicable active ingest key. In one transaction, invalidate every earlier agent token bound to that installation and issue exactly one new installation-bound token scoped to the same operational customer and branch. Failure to revoke old tokens, verify ownership, create the new token, or write its audit record must roll back the transaction. Neither the raw token nor its hash may enter audit records. Inactive and Retired installations are ineligible. The raw recovery token may be displayed once for the existing secure installer-resume process.

## 11. Acceptance criteria

C6a implementation is complete only when:

- A platform administrator can explicitly authorize replacement of an existing Contract v2 installation.
- Ordinary enrollment restrictions remain enforced.
- Old installation credentials no longer authorize uploads or heartbeats.
- The replacement receives new installation-scoped credentials.
- A second active Collector for the same branch is refused.
- The original branch cutover and historical event records remain intact.
- The replacement Collector completes installation and sends a successful heartbeat.
- Ingestion resumes under the original customer and branch: from rebound cursors in Mode 1, and from end-of-file in Mode 2.
- Abandoned or failed replacement attempts have a documented recovery process.
- All credential and lifecycle changes are audited without exposing secret material.
- Authorization, RLS, concurrency, rollback, and compatibility tests pass.

### Data-integrity acceptance criteria

C6a cannot be considered commercially complete until:

- A planned replacement's ingestion behavior is tested across the credential cutover.
- An unplanned failure's data-loss behavior is measured and documented.
- Historical and new-key hold events are tested for double-counting and failed retractions.
- Reporting has an approved mitigation for any unresolved continuity limitation.
- An installation that never sends its first heartbeat remains visible to operational monitoring.
- Cross-installation token/key misuse is rejected by ingestion endpoints.
- Replacement-specific migration, RLS, rollback, concurrency, and legacy-compatibility tests pass.

### Additional acceptance criteria

- A NULL-owner ingest key retains its existing authorization behavior until deliberate reconciliation.
- Ownership backfills do not silently invalidate active, unbound Collectors.
- Both ingestion endpoints reject mismatched tokens and installation-owned keys.
- Replacement is safely refused or explicitly recovered when the source installation has no active ingest key.
- Monitoring identifies a replacement that heartbeats but fails to perform expected ingestion.
- Monitoring distinguishes a malfunction from a legitimate period with no sorter activity, using validated operational evidence rather than assuming zero uploaded events means failure.
- Failure before redemption leaves the original Collector unaffected in Mode 2, and before source preparation in Mode 1.
- After a Mode 1 fence, cancellation restores the original Collector.
- Failure after redemption does not restore old credentials or produce a second active key.
- The approved data-integrity mitigation is tested before commercial release.

### Monitoring acceptance criteria clarification

Replacement monitoring must identify both:

- A replacement that never sends an accepted heartbeat.
- A replacement that sends healthy heartbeats but fails to ingest records despite evidence of expected sorter activity.

A clean heartbeat or successful Collector run is not, by itself, evidence that the configured source files are correct.

Initially evaluate a server-side comparison of received events against validated site activity patterns. Any resulting alert must be documented as a heuristic and evaluated for false positives during legitimate inactivity.

Additional typed heartbeat diagnostics may be considered if existing server-side evidence is insufficient. Such changes require separate API and Collector compatibility planning.

Under a retained `key_id`, monitoring keyed by ingest key spans both computers. A Mode 1 replacement's health must be judged from installation-level evidence and activation time, not from the key's history alone.

### Mode 1 acceptance criteria

- Mode 1 is refused for every ineligible source, and a refused attempt does not fall back to Mode 2 automatically.
- A partial delivery before the drain blocks export until a clean drain succeeds, and it then produces no duplicate rows.
- After the fence, a source upload or heartbeat is refused, including one racing the fence.
- After activation, source credentials are refused, and only the replacement's token can use the retained `key_id`.
- The retained `key_id` stays active with one owner, no new key is issued, and existing rows are unchanged.
- Events on both sides of the handoff are stored without loss or duplication against a synthetic source file.
- `item_key` and `event_key` values are identical between source and destination for the same input.
- A later correction, A → B → A sequence, and retraction work across the handoff, using the transferred ledger.
- A tampered, wrong-destination, expired, replayed, cancelled, or substituted package is refused before activation consumes anything.
- A truncated, altered, or missing source file fails closed, and no file restarts at offset 0.
- A rules, time zone, or cache mismatch fails validation.
- Every Section 10 failure stage is exercised by crash injection, including a lost activation response that produces exactly one activation.
- A cancelled handoff restores the source, and its package can no longer be used.
- Collector 1.0.14 is safely refused as a handoff destination without consuming the code.
- Package, logs, audit records, and server records contain no plaintext master secret, raw source line, or patron identifier.
- The sealed secret on the destination is protected by a verified ACL and bound to the retained `key_id`.

### Report-ordering acceptance criteria

Before production release, tests on real PostgreSQL must prove through the actual holds report:

- Same-item ties: an equal-time correction, an A → B → A sequence, and a same-second retraction resolve to the greatest `id`.
- Cross-key ties: for the same `item_key` and `event_time` under different `key_id` values, the greatest `id` wins. Mode 1 keeps one key, but the report reads across keys and its rule must hold for all stored rows.
- Totals match an independent `(event_time, id)` reduction across public and ILL holds.

### Mode 2 acceptance criteria

- A Mode 2 replacement receives a new secret and `key_id`, and bootstraps at end-of-file.
- No automatic replay occurs across identities, and server history is unchanged.
- Reports covering an affected range display the discontinuity.
- The limitations in Section 9B are documented and approved before commercial release.

## 12. Scope boundary

General-purpose key rotation, multi-branch provisioning, and changes to legacy Contract v1 enrollment remain outside C6a.

Validated cursor handoff is included for Mode 1 only. Automatic replay across identities, historical-data correction, and cross-key hold reconciliation are excluded. Mode 1 is not a general-purpose way to rotate a key or to move a secret outside an authorized replacement.

No historical event records may be rewritten merely to make a replacement appear successful.

Self-service customer replacement without platform-administrator approval remains outside C6a.

## 13. Decisions and open questions

****Approved October 31, 2026****

- A replacement creates a new installation identity.
- The original installation becomes Retired following successful replacement redemption.
- Credential cutover occurs at enrollment redemption, not administrator authorization. The Mode 1 upload fence is a reversible pause before that cutover, not the cutover itself.
- The original branch cutover and historical event records are preserved.
- Ingestion gaps and cross-key hold inconsistencies require investigation and mitigation-design approval before implementation.
- The selected mitigation must be validated before commercial release.

****Approved October 31, 2026 — Mode 1 and Mode 2 architecture****

These decisions approve the design only. They do not authorize implementation or a production change.

1. **Identity preservation:** Mode 1 retains the existing HMAC master secret and `key_id`, and exclusive ownership of the key moves to the replacement installation.
2. **Upload prevention:** Reuse `inactive`, combined with a verified final drain, in-flight request synchronization, and exclusive ownership enforcement.
3. **Source continuity:** Require validated Tech Logic source-file continuity and cursor rebinding. Fail closed on mismatches, and never silently restart at offset zero.
4. **Handoff package:** Use an authenticated, encrypted, destination-bound, expiring, single-use, technician-carried package, with no server-side HMAC secret escrow.
5. **Mode 2:** Generate a fresh identity, bootstrap at end-of-file, preserve server history, and display an explicit reporting discontinuity. Do not automatically replay historical data across identities.
6. **Configuration:** Preserve the source Collector's exact rules and time zone during Mode 1. Any later change requires a separate validated workflow.
7. **Encryption library:** Prefer Python `cryptography`, subject to dependency, security, and frozen-build validation.
8. **Secret retirement:** Remove operational secrets and credentials from the source after successful activation, require technician attestation, and document residual risks. Do not claim guaranteed physical erasure.
9. **Lost activation response:** Support idempotent recovery through the original handoff transaction without permitting a second activation.
10. **Version compatibility:** Both machines require handoff-compatible Collector versions. Upgrade 1.0.14 and earlier before Mode 1.
11. **Security policy:** Permit secret transfer only through an authorized, encrypted Mode 1 handoff. Never send or store the master secret on the SortView server.
12. **Acceptance tests:** Require real PostgreSQL same-item and cross-key timestamp-tie regression tests before production release.

****Still requiring design approval****

1. Exact schema representation of replacement authorization and installation-to-key ownership.
2. Legacy NULL-owner key reconciliation for both active and retired keys, and recovery of branches without active keys.
3. Database constraints guaranteeing a single active ingest identity and a single pending replacement.
4. Replacement authorization expiry, cancellation, and recovery semantics.
5. Consistent transaction locking order and concurrency guarantees.
6. The open engineering items listed below.
7. Reporting and monitoring behavior during replacement and inactivity.
8. Final administration API, UI, and operational runbook integration.

****Open engineering items for Modes 1 and 2****

Each item has a required invariant in Section 9A, 9B, or 10, but no selected mechanism:

1. How the server serializes the upload fence, activation, and cancellation against in-flight uploads, and that method's cost on the upload path.
2. The source export lock and tombstone, and how a Collector run honors them.
3. The destination registration and activation exchange, and how the source verifies the registered key.
4. The package format and encryption construction.
5. The keyed source-file verification construction.
6. How Tech Logic and its source files migrate, and how sorter downtime is managed.
7. Which identity-relevant configuration and build differences are compatible.
8. How a lost activation response is recovered, and when recovery closes.
9. The installer and bootstrap path for Mode 1, including the handoff-compatible Collector version.
10. The data source and presentation of the Mode 2 reporting discontinuity.
11. The source removal procedure and the form of the technician attestation.
12. The monitoring evidence for a Mode 1 replacement under a retained `key_id`.

****Real-machine and pre-production validation gates****

These are listed in Section 9 under "Before production release of Mode 1".

****Legacy key reconciliation clarification****

Legacy key reconciliation must cover both active and retired ingest keys whose installation ownership is not recorded.

The procedure must verify ownership evidence before backfilling, including any installation-bound tokens and relevant enrollment records. A retired key must not automatically be attributed to an installation merely because it is the most recent key for the branch.

Ambiguous cases require explicit operator reconciliation and must fail closed until resolved.

No schema migration, endpoint, or production change should be made until these decisions have been reviewed.
