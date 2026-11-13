# C6a — Contract v2 Replacement-Machine Design

**Status:** Proposed — design review required  

**Scope:** Support-controlled replacement of an enrolled Contract v2 Collector computer, through Mode 1 controlled handoff or Mode 2 fresh-identity recovery  

**Amendment A (Section 14, design approved 2026-11-12; R0 complete):** Mode 2 is implemented first, through the unchanged Collector 1.0.14 SortView Setup GUI. Where Section 14 is more specific than an earlier section, Section 14 applies to Mode 2. **R1 (authorization and cancellation) is implemented and merged** (PR #288, `d45a2da`; 14.9). **R2 (replacement redemption) is merged** (PR #289, `e5d24a3`) **and deployed** (14.9). R3's design is approved and its feature flag is implemented but not yet merged (14.11); the R3 interface, R4 and R5 are not implemented. Machine replacement has not been qualified for customer use (R4, R5, D6).

**Implementation status:** Mode 1 is unavailable. Mode 2 stages R1 (authorization and cancellation) and R2 (redemption) are merged and deployed (14.9), so a platform administrator can complete a replacement through the administration API. There is no administration UI yet. The server-side gate (14.11) is implemented but not yet deployed, and replacement has not been qualified for customer use. Key ownership and the credential safeguards in Section 2 are implemented (milestones M1 and M2), as are a read-only inventory of legacy keys (milestone M3a) and safeguards in the legacy credential scripts (milestone M3b). Only the behavior listed in Section 2 and the code-inspection findings in Section 9 describe the system as it exists today; everything else here is proposed.

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
- Ingest keys belong to a customer and branch, and may have an owning installation (`ingest_key_ids.installation_id`). A key issued by Contract v2 enrollment is owned by the enrolling installation. Keys issued earlier or by `scripts/issue_ingest_key.py` have no owner.
- An installation that owns an active key cannot be retired through the administration API; it can be deactivated, and offboarding retires installations and keys together.
- `scripts/issue_ingest_key.py` refuses to issue a key for a branch with any active key, any key ever owned by an installation, or a Contract v2 enrollment cutover on record. It refuses to retire an owned key, with no override. A branch with only unowned legacy keys can still retire one and then issue another. Existing duplicate keys and unbound tokens keep working, and no database constraint enforces one active key yet.
- `scripts/create_agent_token.py` refuses a branch with an active owned key. It still permits one whose owned keys are all retired, and it revokes nothing. An unbound token that already exists, or that commits before an enrollment, stays active beside the owned key, which refuses it.
- `scripts/reconcile_ingest_keys.py inventory` reports ingest keys, recorded owners, duplicate or missing active keys, installation and token relationships, enrollment-record evidence and schema differences. It reads one snapshot in a read-only transaction, refuses a role that cannot see every row, and assigns no owner. Its enrollment-record candidates are for review, not proof of ownership.
- A platform administrator can issue a recovery token for an eligible installation (Section 10) and can list and revoke legacy unbound tokens. Neither is an upload fence: a request already past the token check may still finish, and this remains so until the upload synchronization in Section 9A exists.
- Ordinary Contract v2 enrollment is blocked when a branch has an active ingest key or any previous v2 cutover.
- Both enrollment-code issuance and enrollment redemption enforce these restrictions.
- The server stores no Collector HMAC secret; the secret is generated and protected locally.
- Fresh Collector bootstrap starts at the end of each source file.
- `event_key` and `item_key` derive from the master secret, not from `key_id`. Server deduplication is nevertheless scoped to `key_id`: `/v2/upload` uses `ON CONFLICT (customer_id, branch_id, key_id, event_key) DO NOTHING`.
- An installation in `inactive` or `retired` status stops its bound agent token at authentication, for uploads and heartbeats alike. The check takes no lock. It does not stop a legacy unbound token, and it does not change the ingest key. `inactive` can return to `provisioning`, and the next accepted heartbeat makes the installation `active`.
- `/v2/upload` and `/v2/status` check that the ingest key is active and belongs to the token's tenant and, when the key has an owner, that the token is bound to that owner.
- The holds report reads `acs_item_events` without a `key_id` filter and selects each item's latest record by `(event_time, id)`. `id` is a table-wide `BIGSERIAL`, so it orders rows across keys by insertion.
- Check-in and reject reports count stored rows with no `key_id` filter or `DISTINCT`. The application runtime role cannot update or delete event rows.
- A v2 cursor is an offset plus a `(st_dev, st_ino)` file identity. A missing cursor or an identity mismatch makes the reader start that file at offset 0.

The following do not exist today: a Collector drain command, handoff export or import, secret import, cursor rebinding, or a reporting discontinuity display.

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

`ingest_key_ids.installation_id` is a nullable column with a composite foreign key, `(installation_id, branch_id)` referencing `collector_installations (id, branch_id)`, so an owner must be at the key's own branch. It does not cascade (implemented in milestone M1).

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

SortView support must be able to issue a recovery token explicitly bound to the installation. This applies to any eligible Contract v2 installation, not only a replacement, because ordinary enrollment has the same failure. The recovered token must pass the same installation-to-ingest-key ownership checks as the original token.

The existing installer resume and token-storage process should be reused where possible.

A replacement must not be repeated solely because the installer failed to store a token, provided the installation can be securely recovered.

The required installation-bound recovery-token capability is part of C6a implementation scope; the existing unbound token-generation script is insufficient.

**Recovery-token issuance requirements:** The operation must be accessible only to an authorized platform administrator or support role and must produce a durable audit record. It is permitted only for a Provisioning or Active Contract v2 installation that owns its branch's only active ingest key. In one transaction, invalidate every earlier agent token bound to that installation and issue exactly one new installation-bound token scoped to the same operational customer and branch. Failure to revoke old tokens, verify ownership, create the new token, or write its audit record must roll back the transaction. Neither the raw token nor its hash may enter audit records. Inactive and Retired installations are ineligible. The raw recovery token may be displayed once for the existing secure installer-resume process.

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

****Approved for milestones M1 and M2****

These decisions cover key ownership and credential tools only. They do not approve C6a, Mode 1, Mode 2, or any production change.

- **Ownership schema (M1):** The composite foreign key in Section 4, with all existing keys left unowned.
- **Recovery eligibility (M2):** Any eligible Contract v2 installation that owns its branch's only active key, not only a replacement.
- **Revocation (M2):** The new revocation operation is limited to legacy unbound tokens. Installation status and the recovery token govern bound tokens.
- **In-flight requests (M2):** A request authenticated before a token is revoked or replaced may still finish. This is a documented interim limitation until upload synchronization exists, and neither operation is an upload fence.
- **Retirement (M2):** An installation that owns an active key cannot be retired through the administration API. A later replacement cutover must retire its source in its own transaction, not through that route.

****Approved for milestone M3b****

These decisions cover the legacy credential scripts only. They do not approve C6a, ownership stamping, or any production change.

- **Issuance:** An unowned key is refused while the branch has any active key, any key ever owned by an installation, or an enrollment cutover on record, even if its old key has no owner. Issue-then-retire rotation is no longer supported.
- **Retirement:** An owned key cannot be retired through `scripts/issue_ingest_key.py`, with no override.
- **Unbound tokens:** `scripts/create_agent_token.py` refuses a branch with an active owned key, not one with only retired owned keys. Nothing revokes an existing unbound token.

****Still requiring design approval****

1. Exact schema representation of replacement authorization.
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

Apart from milestones M1 and M2, no schema migration, endpoint, or production change should be made until these decisions have been reviewed.

## 14. Amendment A — Mode 2 first, through the existing installer

**Status:** Design approved, 2026-11-12. **R0 is complete, and R1 may begin.**

- **Decided by the product owner:** items 14.1(a) and 14.1(b); D1, D2, D3, D4, D7 and D8 (14.10), subject to compatibility with the existing implementation; and the attribution representation in 14.2 and 14.2a. The installation row is the durable authorization record, and `auth_audit_log` records the lifecycle events.
- **Still open:** D5 and D6, to be decided before R5, and the remaining technical issues at the end of 14.10.
- **R1:** implemented, and all its acceptance criteria passed in local validation on 2026-11-12. Merged to `main` in PR #288 (`d45a2da`). Its record is in 14.9.
- **R2:** implementation decisions A–H approved 2026-11-12 (14.4a). Merged to `main` in PR #289 (`e5d24a3`) and deployed; DigitalOcean reports healthy. Its record is in 14.9.
- **R3:** decisions approved 2026-11-12. The server-side feature flag is approved and implemented as a separate backend-first slice, not yet merged or deployed; the interface is not implemented (14.11).
- **Not implemented:** R3–R5. Replacement has not been qualified for customer use. Design approval does not by itself approve a production change. Each stage ships through its own reviewed PR and meets its acceptance criteria in 14.9.

### 14.1 Scope

(a) **Decided:** Mode 2 (Section 9B) is the first replacement capability. Mode 1 (Section 9A) stays an approved design for a later, continuity-preserving capability and is not part of R1–R5.

(b) **Decided:** the replacement computer is installed with the existing SortView Setup GUI and Collector 1.0.14 bundle (the G2 package), **unmodified**.

(c) Consequences for the server:

- A replacement code is an ordinary enrollment code (same format, hash, one-time claim and time limit), issued for a replacement installation and redeemed at the existing `POST /collector/enroll`.
- A successful redemption returns **exactly** the ordinary Contract v2 response: the same field set and types as today, with the replacement's own `installation_id`, `agent_token` and `key_id`. No field is added, because the setup engine rejects an unexpected response.
- Every replacement refusal uses an existing public refusal code (`enrollment_not_ready` for every replacement-specific reason), returns `409`, and consumes nothing. Setup 1.0.14 then reports "not ready to be enrolled" and says the code was not used.
- The rules and time zone returned are the library's **current** server settings, as in ordinary enrollment. Unlike Mode 1, Mode 2 does not preserve the source's exact rules.

### 14.2 Representation

- **New column:** `collector_installations.replaces_installation_id`, nullable, with no default.
- **Same branch:** a composite foreign key `(replaces_installation_id, branch_id)` referencing `collector_installations (id, branch_id)`. It uses the existing `uq_collector_installations_id_branch_id`, added in M1. A replacement can only replace an installation at its own branch. No cascade.
- **No self-reference:** a CHECK that `replaces_installation_id <> id`.
- **The chain:** a replacement of a replacement links to the installation it replaced, so the chain records every computer the branch has had. Existing rows stay `NULL`, and an installation with `NULL` behaves exactly as today.
- **Library and contract:** the replacement is created in the source's library and branch, with `required_contract = 2`, in `provisioning`. Today the branch is always the library's primary branch, because multi-branch provisioning (C8) does not exist. Authorization verifies that rather than assuming it.
- **One active owner:** the replacement does not own a key until redemption. At every moment, the branch's active key has exactly one owner.
- **Attribution columns,** set together with the link:
  - `replacement_authorized_by_user_id BIGINT`: the administrator who authorized the replacement;
  - `replacement_authorized_at TIMESTAMPTZ`: when they did.

  A CHECK requires all three replacement columns to be set together or all to be `NULL`. `replacement_authorized_by_user_id` has **no foreign key**: it is a historical value, as `tenant_lifecycle_events.actor_user_id` is. That way neither deleting a user nor `ON DELETE SET NULL` can break the CHECK or erase attribution. (No application path deletes `app_users`; the runtime role has no DELETE on it.)
- **Write-once, enforced by the database:** the three replacement columns are written only when authorization inserts the row. A `BEFORE UPDATE` row trigger on `collector_installations` raises if any of them changes. Every other column, including `status`, stays updatable. The runtime role has UPDATE on this table, so the trigger, not code convention, is what makes the link and its attribution immutable. This follows `tenant_lifecycle_events`, which a trigger protects. The general installation create path never sets these columns.
- **Retired is final in every write path.** Replacement depends on a retired source, a cancelled replacement or a replaced installation never becoming usable again. The administration service already allows no transition out of `retired`. `services.tenant_service.update_collector_installation` writes any status with no transition check. It has no caller in `src/`, `main.py` or `scripts/` today, but it could move an installation out of `retired`. R1 removes it or makes it refuse any change out of `retired`.

### 14.2a Attribution and audit (approved 2026-11-12)

**Two places, each doing one job:**

| Purpose | Where | Why |
|---|---|---|
| The link redemption must read, inside its own transaction: which installation is replaced and who authorized it | The write-once columns on the replacement's own `collector_installations` row (14.2) | Read under the redemption's existing locks. Immutable by trigger. Present for as long as the installation row exists |
| The record of each action | `auth_audit_log`, through the existing `log_auth_event_with_connection`, in the same transaction as the change | Every installation administration event already goes there, including status changes, codes and recovery tokens. The runtime role has only SELECT and INSERT on it, so the application cannot change or delete a record. The tenant purge keeps it |

**Events, all with IDs only and never a code, token, hash, hostname or key secret:**
- `admin_replacement_authorized`: actor is the administrator. Records the source and replacement installation IDs, the source's status, and, for a `provisioning` source, `lost_response_confirmed: true` (14.3a).
- `admin_replacement_cancelled`: actor is the administrator. Records the same two installation IDs.
- `collector_replacement_redeemed`: a **system action** (`user_id` NULL). Records `authorized_by_user_id` (copied from the replacement's row), the source and replacement installation IDs, the retired and new key IDs, the enrollment code ID, and the IDs of the tokens deactivated.

Each event commits or rolls back with its change.

**Additional metadata (approved 2026-11-12, R1):** the events also carry these fields. None is a code, token, hash, hostname or key secret.

| Field | Events | Purpose |
|---|---|---|
| `target_org_slug`, `actor_user_id` | Both | The standard fields of every installation administration event |
| `branch_slug` | `admin_replacement_authorized` | The branch the replacement is for |
| `previous_status` | `admin_replacement_cancelled` | The replacement's status before it was retired |
| `revoked_code_count` | `admin_replacement_cancelled` | How many unused enrollment codes the cancellation revoked |

**Not chosen:**
- **Reading the authorizer back from `auth_audit_log` at redemption.** It can only be searched by JSON metadata, it has no protection beyond the role's grants, and it is awaiting a retention policy that could remove the record redemption depends on.
- **`collector_enrollment_codes.created_by_user_id`.** It names whoever issued the *code*, who can differ from the authorizer, and a new code is issued after each expiry.
- **`tenant_lifecycle_events`.** It is trigger-protected, but its event types are a closed list of tenant lifecycle actions, and replacement is not one of them.

**Limits, stated rather than assumed:**
- `auth_audit_log` is append-only for the application role, not trigger-protected. The table owner can still change it, as for every existing administration event.
- A purge deletes the tenant's `collector_installations` rows and with them the write-once columns. The `auth_audit_log` events remain.

### 14.3 Authorization, code issuance, cancellation, expiry

**Authorize.** A platform administrator selects an existing installation and confirms replacement. In one transaction, the server verifies:

1. The library is not cancelled. The source belongs to this library and to its active primary branch, and the source is Contract 2.
2. The source is **enrolled**: it is `provisioning`, `active` or `inactive`, and
3. `active_ingest_key_owners(...)` for the branch is exactly `[source]`. That refuses a key with no owner, several active keys, or a key owned by another installation. Key ownership, not status, decides eligibility (14.3a).
4. The branch has a v2 cutover on record.
5. The branch has no active legacy unbound agent token (decision D4).
6. No other replacement is pending at the branch (14.5).

The server then creates the replacement installation and writes an audit record naming the source and the replacement. It changes no token, key, cutover or source status. Every refusal has a fixed public code.

**Issue a code.** The existing `issue_enrollment_code`, with one change. Its `branch_already_enrolled` refusal is skipped only when the installation has `replaces_installation_id` **and** checks 1–6 still hold under the same locks. An installation without the link is refused exactly as today. Issuing a new code revokes the installation's earlier unused codes, as today. A code expires after the existing time limit, and a new one can be issued for the same pending replacement.

**Cancel (D7, approved direction).** A dedicated **Cancel replacement** operation, before redemption only. Under the same locks as authorization, it verifies that the installation is a pending replacement (14.5), moves it to `retired`, revokes its unused codes, and writes its own audit event naming the source and the replacement. The source keeps running unaffected. A retired installation's codes are already refused at redemption, because only `provisioning` and `active` installations are enrollable. The general status change refuses `retired` for a pending replacement and points to Cancel replacement, so every cancellation has the explicit event. After redemption, cancellation is not possible: see 14.6.

**Expiry (D2, approved direction):** a pending replacement does not expire. Each enrollment code keeps its normal time limit and can be reissued.

### 14.3a Which installations can be a source (D3, approved 2026-11-12)

Redemption never changes the installation's status. An installation becomes `active` at its first accepted contact. That contact is either a scheduled-run heartbeat or a preflight probe (`main.record_installation_heartbeat`). Setup's step 8 runs the interactive and SYSTEM-context preflights, so **a setup that completes moves its installation to `active` before the wizard finishes.** On the server, the four situations look like this:

| # | Situation | What the server shows | Handling |
|---|---|---|---|
| 1 | Provisioning, never redeemed: an ordinary installation, a pending replacement, or an installation added to an already enrolled branch | Owns no key; no used code | **Not a source.** A pending replacement: issue a new code, or Cancel replacement. An ordinary installation on an unenrolled branch: issue a new code. An unlinked installation on an enrolled branch, which can never enroll: retire it with the status change |
| 2 | Redeemed, but the response never reached the computer | `provisioning`, a used code, owns the only active key, no contact | **Replace** (this source) |
| 3 | Redeemed and the response received, but setup stopped before step 8, or is still running; including a token that could not be stored | Identical to 2 | **Not a replacement case.** Run SortView Setup again and choose **Resume setup**. If the token could not be stored, use the existing recovery token for this installation, then resume |
| 4 | Active or Inactive, owns the only active key | `active` or `inactive`; owns the only active key | **Replace** when the computer is failed, lost, compromised (deactivate it first) or being retired |

**The server cannot tell 2 from 3.** Only the computer knows whether it holds the saved enrollment. Setup saves the non-secret enrollment details as soon as it receives the response. When it can't tell whether the request succeeded, SortView Setup stops with **Contact SortView support** and its **Copy details** report, rather than offering Resume.

**Rules (approved):**

- A source must own the branch's only active key and be `provisioning`, `active` or `inactive`. A `retired` installation, an installation that owns no key, and a branch with no active key, or with a key that has no owner, are refused; these need operator reconciliation, outside R1–R5.
- **A `provisioning` source** additionally requires the administrator to confirm that the computer did **not** keep its enrollment: SortView Setup reported an unknown result and offered no Resume setup. The confirmation is recorded in the authorization's audit event as `lost_response_confirmed: true` (14.2a). Without it, authorization is refused, and the refusal tells support to try Resume setup or the recovery token first.
- **Replacing in situation 3 by mistake** is safe for the one-key guarantee: that computer's token stops working at redemption. It only costs the customer the extra steps of a replacement.
- **Repeated replacements:** a redeemed replacement owns the only active key, so it can itself become a source, in either situation 2 (its own lost response) or situation 4 (a later failure). That is the whole chain rule. No other installation is ever eligible.
- **A pending replacement is never a source:** it owns no key.

### 14.4 Redemption: order and locks

> **Superseded lock order.** The R2 assessment (2026-11-12) found that this table's order can deadlock with an upload already in progress from the source computer. **14.4a replaces the order below**; the actions, guards and outcomes are unchanged except where 14.4a says otherwise.

Redemption of a code whose installation has `replaces_installation_id` runs in **one** transaction. It extends the existing redemption, keeps its lock order, and adds the source's credentials using the order the recovery token already uses (tokens before installation):

| Step | Action | Existing or new |
|---|---|---|
| 1 | Organization `FOR SHARE`, from the code (`_lock_code_organization`) | Existing |
| 2 | Code row `FOR UPDATE`; reject unknown, used, revoked or expired codes | Existing |
| 3 | Replacement installation scope; must be `provisioning` or `active` | Existing |
| 4 | Branch row `FOR UPDATE` (`_prepare_v2_enrollment`), tenant context set, replacement status re-read under the lock | Existing |
| 5 | Settings, time zone and rules checks | Existing |
| 6 | Instead of the `active_ingest_key_exists` and `cutover_exists` refusals: re-run checks 1–6 of 14.3 for the linked source. Any failure → `enrollment_not_ready`, nothing consumed | **New** |
| 7 | Claim the code (exactly one row) | Existing |
| 8 | Deactivate every agent token bound to the source | **New** (the recovery token's statement) |
| 9 | Lock the source installation row, then move it to `retired`, guarded on the status read in step 6 (exactly one row) | **New** |
| 10 | Retire the source's key: `status = 'retired'`, `retired_at` set, guarded on `key_id`, `installation_id = source` and `status = 'active'` (exactly one row). `retire_ingest_key` is not used: it refuses owned keys by design | **New** |
| 11 | Issue the new key, owned by the replacement (`issue_ingest_key`) | Existing |
| 12 | Insert the new bound agent token | Existing |
| 13 | **No** `record_initial_v2_cutover`. The branch's cutover is never written, moved or recreated | **New** (skipped) |
| 14 | `collector_replacement_redeemed` audit event, a **system action** (D8). The authorizing administrator is read from the replacement's write-once `replacement_authorized_by_user_id`, already read in step 3. Contents in 14.2a | **New** |
| 15 | Return the ordinary v2 response (14.1(c)) | Existing shape |

An ordinary enrollment is unchanged, including both refusals that step 6 replaces for replacements only.

**Why this order:**
- Offboarding takes the organization `FOR UPDATE`, and so do recovery and revocation. Enrollment takes it `FOR SHARE`. Any of them serializes against redemption.
- A heartbeat holds its token and then updates its installation. Steps 8–9 take tokens before the installation in the same order, as the recovery token does, so they cannot deadlock with it.
- Administrative retirement takes the branch `FOR NO KEY UPDATE`, which conflicts with step 4's lock.
- Event inserts take the branch `FOR KEY SHARE`, so a branch's uploads wait for the redemption to commit, exactly as during an ordinary enrollment today.
- The authorization transaction takes the organization `FOR UPDATE` and then the branch, so authorization, code issuance and redemption cannot interleave.

### 14.4a R2 implementation decisions and the corrected lock order (approved 2026-11-12)

**The defect in 14.4.** An upload from the source computer updates its own token row (`agent_tokens.last_used_at`, in `main._authenticate_agent`) and then inserts events. The v2 event tables reference `branches(id)`, so each insert takes the branch `FOR KEY SHARE`. The 14.4 order holds the branch `FOR UPDATE` (step 4) and then waits for the source's token (step 8): each transaction waits for the other. PostgreSQL would abort one of them. If it aborted the redemption, the setup app would get a 500, an "unknown result", although the code was not used.

**Decisions:**

| | Decision |
|---|---|
| A | The redemption takes the source's tokens and the source installation **before** the branch (order below) |
| B | Check 6 at redemption: no **other** pending replacement at the branch, excluding the one being redeemed |
| C | The source's status is re-read under its row lock. `provisioning`, `active` or `inactive` is accepted if every source eligibility check still passes. The lost-response confirmation belongs to the authorization and is not asked again |
| D | The source eligibility checks move to one small shared module, used by R1 and by redemption. R1's behavior and refusal codes are unchanged |
| E | The `collector_replacement_redeemed` event also carries `target_org_slug` and `branch_slug`. Never a code, token, hash, hostname or key secret |
| F | Unexpected errors keep today's behavior: a 500, with everything rolled back. A deadlock, lock timeout or failed guarded update is **never** reported as an eligibility refusal |
| G | R2 may be developed, tested and submitted for review, but is **not merged or deployed** until D6 is decided. No feature flag while deployment is blocked. **Superseded:** the product owner merged and deployed R2 (PR #289) before D6; the gate is now the server-side flag proposed in 14.11 |
| H | A suspended library's redemption is refused as today (generic `400`, `organization_status_suspended`), before any write. R2 tests prove it consumes no code and changes no credential |

**Row-level security (pre-implementation check 1).** Only the six event tables and `ingest_key_ids` have row-level security (migration `0acba192bf69`; `scripts/runtime_role_privileges.RLS_TABLES`).
- `agent_tokens` and `collector_installations` have none, so the runtime role can lock the source's tokens and installation with no tenant context. Its table-level UPDATE privilege is what `SELECT … FOR NO KEY UPDATE` requires.
- The tenant context is nevertheless set right after the replacement's scope is resolved, before any lock, from the validated scope.
- The source is at the replacement's branch, guaranteed by the same-branch foreign key, so the same context covers the source's key.

**Corrected order.** A linked code takes this path; an unlinked code is today's enrollment, unchanged.

| Step | Action | Lock taken |
|---|---|---|
| 1 | Organization of the code (`_lock_code_organization`) | organization `FOR SHARE` |
| 2 | Code row; refuse unknown, used, revoked or expired | code `FOR UPDATE` |
| 3 | Replacement scope (`resolve_installation_scope`), its `replaces_installation_id` (write-once, safe to read unlocked), the contract check, then the tenant context | — |
| 4 | The source's bound agent tokens, in `id` order | each `FOR NO KEY UPDATE` |
| 5 | The source installation row | `FOR NO KEY UPDATE` |
| 6 | The replacement installation row; its status re-read: must be `provisioning` or `active` | `FOR NO KEY UPDATE` |
| 7 | Branch (`_prepare_v2_enrollment`); settings, time zone and rules | branch `FOR UPDATE` |
| 8 | Source eligibility under these locks (decision D), the source status re-read (C), and no other pending replacement (B). Any failure is `EnrollmentRefused(enrollment_not_ready, <reason>)` and consumes nothing | the eligibility module's branch `FOR NO KEY UPDATE`, weaker than the lock already held |
| 9 | Claim the code (exactly one row) | — |
| 10 | Deactivate the source's tokens (already locked) | — |
| 11 | Source → `retired`, guarded on the status read in step 8 (exactly one row) | — |
| 12 | Source key → `retired`, `retired_at` set, guarded on `key_id`, `installation_id = source` and `status = 'active'` (exactly one row) | key row (update) |
| 13 | Issue the replacement's key (`issue_ingest_key`) and insert its bound token | `FOR KEY SHARE` on the replacement row and the branch (foreign key checks; already held more strongly) |
| 14 | **No** cutover write | — |
| 15 | `collector_replacement_redeemed`, a system action (14.2a, E) | — |
| 16 | The ordinary nine-field v2 response | — |

**Rows are locked `FOR NO KEY UPDATE`, not `FOR UPDATE`.** No key column is changed, so foreign key checks from other transactions (`FOR KEY SHARE`) are not blocked.

**Lock dependency table (pre-implementation checks 2 and 3).** Each row is the order in which an operation takes locks that redemption also takes.

| Operation | Order of locks shared with redemption | Organization lock first? |
|---|---|---|
| **Replacement redemption** (above) | org S → code → source tokens → source installation → replacement installation → branch U → key row → FK key-share on the replacement and branch | — |
| v2 upload (`/v2/upload`) | its token → branch KS (event insert) → the branch's onboarding update, only while `pending` | no |
| v2 heartbeat (`/v2/status`) | its token → its key row → its installation | no |
| v1 upload / v1 heartbeat | its token → branch KS (insert) → its installation (heartbeat) | no |
| Ordinary enrollment | org S → code → branch U → FK key-share on its own installation | yes (S) |
| Recovery token | org U → the installation's tokens → that installation | yes |
| Revoke an unbound token | org U → that token | yes |
| Authorize / cancel / issue a code (R1) | org U → installation or branch NKU → FK key-share | yes |
| Status change: retire | branch NKU → that installation | no |
| Status change: deactivate, reactivate, rename | that installation | no |
| Offboarding | org U → tokens → installations → codes → keys | yes |
| Unowned key issue (operator) | org S → branch NKU | yes (S) |

Key: S `FOR SHARE`, U `FOR UPDATE`, NKU `FOR NO KEY UPDATE`, KS `FOR KEY SHARE`.

**No cycle with the corrected order:**
- **Every operation that takes the organization `FOR UPDATE` takes it first.** Redemption holds it `FOR SHARE` from its first statement, so these operations run strictly before or after a redemption: recovery, revocation, R1 authorization, cancellation and code issuance, and offboarding.
- **Ordinary enrollment and unowned key issue** share the organization `FOR SHARE` and touch none of the source's rows. Each waits for the branch while holding nothing redemption needs.
- **Uploads and heartbeats** take their token first and nothing before it. Redemption takes the source's tokens before any installation, branch or key row.
  - If the request holds the token, redemption waits while holding only the organization and the code, which no request needs.
  - If redemption holds the token, the request waits while holding nothing.
  - Requests with other tokens at the branch need nothing redemption holds.
- **The status change takes no organization lock**, and holds at most the branch and one installation, branch first.
  - Retiring the source or a pending replacement is refused after the branch lock, before any installation lock.
  - Retiring another installation holds the branch, then that installation, which redemption never locks.
  - Deactivating, reactivating or renaming holds one installation and nothing else.

  So redemption, holding installations and waiting for the branch, never waits for a holder of something it has.
- **Among installation rows, redemption alone holds two**, always the source before the replacement.
- **The source's token rows** are taken in `id` order. Every other holder of a source token holds one token, or holds the organization lock.

**Guarded updates and rollback (pre-implementation check 4).**
- Steps 9–12 each require exactly one row.
- Under the locks above, zero rows means something broke an invariant. Redemption raises an internal error, which is not an `EnrollmentRefused`. The enroll route's existing `except Exception` returns a 500, and `engine.begin()` rolls back the whole transaction, so the code stays unused and no credential changes (decision F).
- The code claim keeps its existing `lost_redemption_race` refusal.
- Tests force each guarded update to affect zero rows and check that nothing changed.

**What is preserved (pre-implementation checks 5 and 6):**
- The response is the ordinary nine fields (`setup-collector.ps1` `V2ResponseKeys`): `customer_id`, `branch_id`, `installation_id`, `agent_token`, `contract_version`, `contract_mode`, `key_id`, `timezone`, `classification_rules`.
- No statement touches an event table.
- No `v2_cutovers` row is written, so the v1 `/upload` stays closed and the boundary does not move.

**Remaining limitation (14.5).** A request that read its token and checked its key before the commit can still finish. One that checks the key afterwards sees it retired and is refused with 403.

### 14.5 Atomicity and concurrency guarantees

- **All or nothing:** any failure at steps 1–15 rolls back the whole transaction. The code stays unused, the source keeps its status, tokens and key, and no key or token is created. Every guarded update must affect exactly one row; zero rows is a failure, not success (Section 6).
- **One active Collector:** after commit, the source's bound tokens are inactive, its installation is retired, and its key is retired. `/v2/upload` and `/v2/status` refuse it on all three grounds. The only token that can use the branch's single active key is bound to the replacement.
- **Earlier requests:** a request that passed authentication before the commit may still finish under the old key. This is the interim limitation recorded for M2. Under Mode 2 those rows are genuine events from the source and are kept.
- **Races:** two redemptions for one branch, or a redemption against an ordinary enrollment, recovery-token issuance, administrative retirement or offboarding, serialize on the locks in 14.4. Exactly one outcome commits, and every other request sees it and is refused.
- **Pending replacements (D1, approved direction):** at most one per branch, enforced by checks under the existing locks and proved by concurrency tests. No new constraint. A **pending replacement** is an installation that has `replaces_installation_id`, is not `retired`, and owns no ingest key, active or retired. `inactive` counts as pending: otherwise deactivating one pending replacement, authorizing a second and reactivating the first would leave two. The rule holds only because every path that could add a pending replacement is serialized, as follows:

  | Path | Effect on pending replacements | Why the rule still holds |
  |---|---|---|
  | Authorize | Adds one | The only writer of `replaces_installation_id`. Takes the organization `FOR UPDATE` before checking |
  | Cancel replacement | Removes one | Same lock |
  | Redemption | Removes one (it now owns a key) | Holds the organization `FOR SHARE`, which conflicts with the two above |
  | Offboarding | Removes all | Organization `FOR UPDATE` |
  | Status change: `provisioning` ↔ `inactive` | None (both count) | — |
  | Status change: `retired` | Would remove one | Refused for a pending replacement (Cancel replacement instead) |
  | Ordinary "Add an installation" | None: it never sets the link | The link is immutable |
  | Heartbeat or recovery token | None: a pending replacement has no token and owns no key | — |
  | `tenant_service.update_collector_installation` | Could revive a cancelled replacement or a retired source | Removed or guarded in R1 (14.2) |
- **Constraint:** a database constraint enforcing one active key per branch (open item 3 in Section 13) is **not** part of R1. It follows the production key inventory (`scripts/reconcile_ingest_keys.py inventory`), because duplicate legacy keys may exist (Section 2).

### 14.6 Interrupted replacement and lost responses

| Situation | Outcome |
|---|---|
| Authorized, no code yet, or code expired | Issue a new code for the same pending replacement. The source is unaffected |
| Redemption refused (`enrollment_not_ready`) | Nothing consumed. Support fixes the cause (for example, revokes an unbound token) and the installer retries with the same code before it expires |
| Abandoned before redemption | Cancel (14.3). The source is unaffected |
| Setup stops after a successful redemption, with the response received (14.3a, situation 3) | The existing 1.0.14 resume: run SortView Setup again and choose **Resume setup**. No new code, and no replacement |
| The token could not be stored after redemption (situation 3) | The existing recovery token for the **replacement** installation, then Resume setup. It is eligible because it owns its branch's only active key and is `provisioning` or `active` |
| Response lost: SortView Setup reports an unknown result and offers no Resume setup (situation 2) | The code is used, and the replacement owns the active key but its computer holds nothing. A recovery token cannot help: the computer also lacks the key ID, rules and time zone. Recovery is **another authorized replacement whose source is the unconfirmed replacement**, with the `provisioning` confirmation in 14.3a. Never retry a used code |
| The replacement computer fails later | Another replacement, with that computer's installation as the source |

**No rollback after redemption.** The commit of step 15 is the rollback boundary. A retired installation cannot return to `provisioning` (`retired` is terminal today). No path reactivates the source's tokens, its key or its status, and no path creates a second active key or a second cutover.

**The source computer:** after redemption it can still run, but every upload and heartbeat is refused. The runbook requires a support step: disable its `SortView Collector` task and run `tools\uninstall.ps1`. Its local secret is useless to the server once its key is retired.

**To verify in R4 rather than assume:** that a computer whose enrollment response was lost is still clean enough for a new run of SortView Setup.

### 14.7 Historical data, and what Mode 2 does not guarantee

**Preserved:**
- **Events:** every stored event row is kept unchanged. The runtime role cannot update or delete event rows.
- **Keys and installations:** the retired key and the retired source installation are kept, not deleted.
- **The cutover:** the branch's `v2_cutovers` row is never written by a replacement. The runtime role has only `SELECT` and `INSERT` on that table.
- **Reports:** they read events without a `key_id` filter, so data from before the replacement stays visible.

**Not guaranteed. Mode 2 does not provide gap-free ingestion:**
- **Records never ingested:** lines Tech Logic wrote after the source's last committed position, and lines written before the replacement's bootstrap (which starts at the end of each file), are never ingested.
- **Holds across the boundary:** a hold recorded before the replacement cannot be retracted or corrected by a later record, because item identities change with the new secret.
- **Double counting:** one physical item can be counted under both identities in a range that spans the replacement.

**Reducing the gap (runbook, not a guarantee):** for a planned replacement, stop the sorter, let the source finish one successful run, and confirm it in SortView Administration before the code is redeemed.

### 14.8 Before commercial distribution

R1–R4 may only be used under decision D6. Before replacement is offered to customers generally (R5), all of the following must hold:

1. **Discontinuity shown:** reports and the dashboard show an explicit discontinuity for any range that spans a replacement (Section 9B). Decision D5 chooses the data source and presentation.
2. **Unfinished replacements visible:** support can see a replacement that was redeemed but never sent a heartbeat, and one that sends heartbeats but ingests nothing despite expected sorter activity (Section 11, *Monitoring*).
3. **Customer documentation:** the customer guide and the internal onboarding checklist describe replacement, in particular what happens to the old computer, the possible data gap, and that a used code is never reused.
4. **Support runbook:** authorize, issue a code, cancel, recover a lost response, and retire the old computer, with the audit records each step leaves.
5. **Limitations approved:** the Mode 2 limitations in 14.7 are approved by the product owner and stated to the customer.

### 14.9 Implementation stages

Each stage is its own PR and deploys independently. No stage adds Mode 1, key rotation, multi-branch provisioning or a change to the installer.

**R1 — authorization and cancellation** (migration, service, admin API, audit, tests). Acceptance:
- The migration adds 14.2 and is reversible; existing rows are unchanged.
- Authorization succeeds only when every check in 14.3 holds. It creates one linked `provisioning` installation with an audit record and changes nothing else. Each refusal has a fixed code and a test.
- A code can be issued for a linked installation. An unlinked installation on an enrolled branch is still refused with `branch_already_enrolled`.
- **Redeeming a replacement code is still refused with `enrollment_not_ready`, and the code is not consumed.** This shows R1 cannot enable replacement on its own. The internal reason is `active_ingest_key_exists`: the source owns the branch's active key, and that check runs before `cutover_exists`. (Corrected 2026-11-12; the requirement that the code stays unused is unchanged.)
- Cancel replacement retires the pending replacement with its own audit event, and its code is then refused. The general status change refuses to retire a pending replacement.
- Source eligibility follows 14.3a. Each of situations 1–4 is tested, including a refused `provisioning` source without the confirmation.
- Concurrency tests show two authorizations for one branch, an authorization against Cancel replacement, an authorization against offboarding, and the deactivate–authorize–reactivate sequence all leave at most one pending replacement.
- No path moves an installation out of `retired`, including `tenant_service.update_collector_installation` (removed or guarded).
- On PostgreSQL, the write-once trigger refuses any update to the three replacement columns, including one made as the runtime role, and allows every other column. The all-or-none CHECK refuses a partial link.
- `admin_replacement_authorized` and `admin_replacement_cancelled` are written in the same transaction as their change, contain only the fields in 14.2a, and roll back with it.
- All existing enrollment, installation, recovery and RLS tests pass unchanged.

**R1 status: implemented, and every R1 acceptance criterion above passed in local validation on 2026-11-12. Merged to `main` in PR #288 (`d45a2da`).** Machine replacement is not available: redeeming a replacement code is refused until R2.

- **Migration `b8e2d4f61a07`** (after `c1e7a94f2b60`; single head). It adds:
  - the three columns in 14.2;
  - `fk_collector_installations_replaces_same_branch` (NO ACTION, no cascade);
  - `ck_collector_installations_replacement_all_or_none` and `ck_collector_installations_replaces_not_self`;
  - the trigger `trg_collector_installations_replacement_write_once`.

  Its downgrade removes exactly these.
- **Endpoints** (platform administrator, admin origin guard):
  - `POST /api/admin/libraries/{org_slug}/installations/{installation_id}/replacements`: authorize, `201`. Body `name`, `confirm_lost_response` (default `false`).
  - `POST /api/admin/libraries/{org_slug}/installations/{installation_id}/cancel-replacement`: cancel, `200`.
- **Service** (`services/admin_installation_service.py`): `authorize_replacement` and `cancel_replacement` take the organization `FOR UPDATE`, then the branch.
  - `issue_enrollment_code` allows a code for a linked installation only while the source is still eligible.
  - `change_status` refuses to retire a pending replacement (`use_cancel_replacement`).
- **Retired is final:** `tenant_service.update_collector_installation` now refuses any change out of `retired`.
- **Attribution and audit:**
  - The write-once trigger refuses any change to the three columns, for the runtime role and the owner alike.
  - `admin_replacement_authorized` and `admin_replacement_cancelled` are written through `log_auth_event_with_connection`, in the change's transaction. They carry only the fields in 14.2a, including the approved additional metadata.
- **Replacement chains and the tenant purge:** the self-referencing foreign key is compatible with `scripts/purge_tenant_data.py`.
  - The purge deletes a tenant's installations in one statement.
  - A NO ACTION key is checked at the end of the statement.
  - The key includes `branch_id`, so a chain never leaves the tenant.
  - PostgreSQL tests check this: the purge-order test requires a self-reference to stay NO ACTION and include `branch_id`; a chain A → B → C is purged in full; deleting the middle of a chain is refused.
- **Replacement enrollment remains disabled until R2:** redemption is unchanged and refuses a replacement code without consuming it.
- **Final local validation (2026-11-12; local disposable PostgreSQL 18, never production):**
  - **Full PostgreSQL opt-in suite: 489 passed, 0 failed.** It includes the audit-rollback tests, the migration round trip, the exact redemption refusal, and the tenant-purge replacement-chain tests.
  - Full Python suite without PostgreSQL: 11,290 passed, 1,024 skipped, 0 failed. This run predates the three PostgreSQL-only tests, which are skipped without a database.
  - ruff and bandit clean on changed files; `git diff --check` clean.
- **Deployment order:** apply `b8e2d4f61a07` to production **before** this code goes live.
  - The admin status change and code issuance read `replaces_installation_id` for every installation, so on an unmigrated database they fail.
  - The migration is safe under the current code: it only adds, and existing rows stay `NULL`.
  - Merging to `main` deploys automatically, and the schema compatibility guard (`docs/deployment.md`) is not yet configured, so this order is a manual step.
- **Redemption refusal:** the test asserts the public `enrollment_not_ready`, the exact internal reason `active_ingest_key_exists`, and an unused code.
- **Three existing tests were changed (approved as justified):**
  - **The two route inventories** (`tests/test_admin_api.py`, `tests/test_customer_api_import_seam.py`). They list every admin route on purpose, so that a new route must be added deliberately and is checked for the platform-administrator and admin-origin guards. They gained the two new routes; no existing entry changed.
  - **The tenant-purge foreign-key test** (`tests/test_tenant_lifecycle_postgres.py`). It assumed every foreign key joins two different tables, purged child first. A self-reference cannot satisfy that.
    - The test now requires such a key on a purged table to be NO ACTION and to include `branch_id`, the two properties that make a single-statement purge safe.
    - Every cross-table check is unchanged.
- **Added for the remaining criteria. Passed on PostgreSQL, 2026-11-12:**
  - **Audit rollback** (`tests/test_admin_replacement_postgres.py`): a failure is injected after the audit insert, inside the transaction. Two tests, one for authorization and one for cancellation, show:
    - the event was visible to the transaction;
    - after the rollback, the event, the installation, its status and its codes are exactly as before.
  - **Migration round trip** (same module), on its own throwaway database:
    - upgrade from `c1e7a94f2b60`, then check that the upgrade adds exactly the three columns, three constraints, the trigger and its function, and leaves existing rows unset;
    - check that each constraint and the trigger is enforced;
    - downgrade to `c1e7a94f2b60`, then check that every column, constraint, index, trigger, function and row in the public schema matches its state before the upgrade;
    - upgrade to head again.

**R2 — replacement redemption** (enrollment service). Acceptance:
- 14.4 as corrected by 14.4a, and 14.5, tested on SQLite and on PostgreSQL under the runtime role with RLS.
- An upload or heartbeat from the source already in progress, holding its token, does not deadlock with redemption (14.4a, A).
- A suspended library's redemption consumes no code and changes no credential (14.4a, H).
- R1's tests pass unchanged after the eligibility checks move to the shared module (14.4a, D).
- After commit: the old token is refused on both v2 endpoints, the old key is refused, the new token and key are accepted, exactly one active key exists, the cutover rows are unchanged, and event row counts are unchanged.
- Fault injection at each step of 14.4 leaves everything unchanged with the code unused.
- Concurrency tests cover every race in 14.5.
- Chains work: A → B → C, and recovery from a lost response by replacing the unconfirmed installation.
- The replacement response has exactly the ordinary v2 field set, checked against the setup engine's parser. A Contract 1 or 1.0.13 request is refused without consuming the code.
- No code, token or secret appears in logs or audit records.

**R2 status: every R2 acceptance criterion above passed in local validation on 2026-11-12. Merged to `main` in PR #289 (`e5d24a3`) and deployed; R1's migration `b8e2d4f61a07` was applied to production first. Not yet qualified for customer use (R4, R5, D6), and not yet gated (14.11).** No migration, no installer or Collector change.

- **Code:**
  - `services/replacement_eligibility.py` (new): the source rules, the primary branch, the branch lock, and the pending-replacement query with an exclusion. R1's codes are unchanged.
  - `services/admin_installation_service.py`: uses that module. R1's behavior and refusal codes are the same.
  - `services/collector_enrollment_service.py`:
    - `InstallationScope.replaces_installation_id`;
    - `_redeem_replacement`, in the 14.4a order;
    - `_prepare_v2_enrollment(replacement=True)` skips only the two "branch already enrolled" refusals;
    - `ReplacementCutoverError` for a guarded write that changes no row;
    - the `collector_replacement_redeemed` audit event.
  - A linked code with a Contract 1 snapshot is refused with `enrollment_not_ready` (`replacement_requires_contract_2`). It cannot occur under R1 and is guarded anyway.
  - `main.py` is unchanged: the enroll route's 409, 400 and 500 mapping and the ordinary enrollment are as before.
- **Tests:**
  - `tests/test_replacement_redemption.py` (SQLite, 30 tests).
  - `tests/test_replacement_redemption_postgres.py` (21 PostgreSQL tests). They cover the runtime role under row-level security, the upload and heartbeat already holding the source's token (decision A), every race in the matrix, failure injection, the suspended library, and lost-response recovery through R1's real authorization.
- **Existing tests changed, and why:**
  - `test_admin_replacement_postgres.py`: R1's interim assertion that a replacement code is refused (`active_ingest_key_exists`) is replaced by the R1→R2 handoff. The code R1 issues is redeemed, the replacement is no longer pending, and Cancel replacement no longer applies. That refusal was R1's deliberate interim behavior, which R2 removes.
  - `test_collector_enrollment.py`, `test_collector_enrollment_v2.py`, `test_library_offboarding.py`: their hand-written SQLite `collector_installations` tables gain the `replaces_installation_id` column the enrollment now reads, as they gained `required_contract` in A1. No assertion changed.
  - Found by the first full PostgreSQL run of R2 (1,046 passed, 2 failed); neither failure was caused by R2:
    - **`test_ingest_v2_postgres.py`:** its exact inventory of every trigger in the database predated R1's write-once trigger. That trigger is now listed. The inventory stays exact, so any other trigger still fails the test.
    - **`test_admin_library_status_postgres.py`:**
      - **Cause:** its role is granted a hand-written list of tables. `GET /api/admin/libraries/{slug}` has also read `memberships` and `app_users` since the first-owner assignment (`dbe06cd`, before R1), so the role got `InsufficientPrivilege` (42501). Production's runtime role has `SELECT` on both (`scripts/runtime_role_privileges.BASELINE`).
      - **Fix:** the two tables are granted, and the test now checks the owner flag read through them.
      - **New test:** every table the fixture grants must be one the production runtime role may `SELECT`.
- **Final local validation (2026-11-12; local disposable PostgreSQL 18, never production):**
  - **Full PostgreSQL opt-in suite: 1,049 passed, 0 failed**, after the two test-only fixes above. It includes the 21 PostgreSQL redemption tests.
  - **Full Python suite without PostgreSQL: 11,320 passed, 1,049 skipped, 0 failed.** An earlier run had 9 failures, all from one test name that broke the repository's freshness rule (a release number in the name); the test was renamed.
  - ruff clean; bandit clean (the CI command); `git diff --check` clean.
- **Where each criterion is tested:**

  | Criterion | Tests |
  |---|---|
  | 14.4a order and 14.5, SQLite and PostgreSQL runtime role under RLS | `test_replacement_redemption.py` (all); `test_the_cutover_commits_as_the_runtime_role_under_row_level_security` |
  | An upload or heartbeat in progress is not deadlocked (A) | `test_an_upload_in_progress_holding_the_source_token_is_waited_for_not_deadlocked`, `test_a_heartbeat_in_progress_…` |
  | Suspended library (H) | `test_a_suspended_library_…` (SQLite and PostgreSQL) |
  | R1 unchanged after the shared module (D) | `test_admin_replacement.py`, `test_admin_replacement_postgres.py`: unchanged except R1's interim refusal, replaced by the handoff test (above) |
  | After commit: source refused, replacement accepted, one active key, cutover and events unchanged | `test_after_cutover_the_source_is_refused_everywhere_…`, `test_after_commit_the_source_is_refused_…`, `test_redemption_moves_the_branchs_identity_…` |
  | Fault injection, guarded writes (F) | `test_a_failure_at_any_write_…` (claim, key issue, token insert, audit), `test_a_guarded_write_that_changes_no_row_…` (source, source key), on both databases |
  | Every race in 14.5 | every `test_redemption_racing_…` test and `test_two_redemptions_of_one_code_…` (PostgreSQL) |
  | Chains and lost response | `test_a_chain_of_replacements_…` (SQLite); `test_a_lost_response_is_recovered_…` (both databases, a three-installation chain on PostgreSQL) |
  | Exact v2 field set; Contract 1 refused unconsumed | `test_the_response_is_exactly_the_field_set_the_setup_engine_accepts`, `test_an_old_installer_is_refused_…` |
  | No secret in logs or audit (E) | `test_the_redemption_is_audited_as_a_system_action_…`; the stored row on PostgreSQL |
- **Two refusals beyond the 14.4a table**, both `enrollment_not_ready` and consuming nothing, guarding states R1 cannot produce: `replacement_requires_contract_2` and `replacement_already_owns_a_key`.
- **Deployment:** no migration of its own, but every enrollment reads `replaces_installation_id`, so R1's migration `b8e2d4f61a07` had to be applied first. It was.

**R3 — administration UI, feature flag and runbook** (14.11). "Replace this Collector computer" and "Cancel replacement" in the React admin app, showing a pending replacement and its source. The server-side replacement flag, once its design is approved. Plus the support runbook (14.8, item 4): `docs/c6a-replacement-support-runbook.md`. Acceptance:
- With the flag off (the default): authorization, a code for a pending replacement, and redemption of a replacement code are refused, each consuming and writing nothing; cancellation still works; ordinary enrollment, code issuance, recovery tokens, uploads and heartbeats are unchanged.
- With the flag on: R1's and R2's tests pass, with only the change that turns the flag on in their fixtures.
- The installation list carries `replaces_installation_id`, the authoritative `pending_replacement_id` (read as the runtime role under row-level security) and `replacement_enabled`.
- UI tests for the workflow in 14.11, including that the raw code is kept out of the query cache, storage and the page after Done or leaving.
- The accessibility checks used for the rest of the admin app (axe), with a pending replacement and each open form.
- A runbook review.

**R4 — VM qualification with the unchanged G2 package.** Acceptance:
- A first replacement from an active, ingesting source, using installation `9`, is documented. Afterwards:
  - the source's next scheduled run is refused and inserts nothing;
  - the replacement's first automatic run gives `0` and it becomes Active;
  - appended synthetic records are ingested;
  - the database shows the source and its key retired, one active key owned by the replacement, the source's rows unchanged, and the cutover unchanged.
- A second replacement (replacement → third computer) works.
- One interrupted-after-redemption resume works, and a lost response is recovered.

**R5 — commercial gate.** Everything in 14.8 is implemented, tested and approved.

### 14.10 Decisions needed before R1

| ID | Decision | Options | Proposed |
|---|---|---|---|
| D1 | How to enforce at most one pending replacement per branch | (a) a check under the locks in 14.4, as one active key is enforced today; (b) a redemption marker column plus a partial unique index | **Approved direction: (a)**, with concurrency tests; definition and path analysis in 14.5 |
| D2 | Does the authorization expire? | (a) no, it lasts until cancelled or redeemed (codes still expire); (b) a time limit on the authorization | **Approved direction: (a)** |
| D3 | Which installations can be a source? | See 14.3a | **Approved 2026-11-12:** the owner of the branch's only active key, in `provisioning`, `active` or `inactive`, never `retired`; a `provisioning` source needs the lost-response confirmation, recorded in the authorization audit |
| D4 | Active legacy unbound tokens at the branch | (a) refuse, and support revokes them with the existing route; (b) revoke them automatically at redemption | **Approved direction: (a)** |
| D5 | Data source and presentation of the reporting discontinuity | For example, derived from the retired key's `retired_at` and the new key's `created_at` in `ingest_key_ids`, or from the replacement audit record | Decide before R5; not needed for R1 |
| D6 | May supervised-pilot customers use replacement before R5? | Yes, with the 14.7 limitations stated and support performing every step; or no | Product owner decision |
| D7 | Cancellation | (a) the existing status change to `retired`, audited as a status change; (b) a dedicated cancel operation with its own audit event | **Approved direction: (b)**; see 14.3 |
| D8 | Audit record at redemption | Redemption has no administrator. Record the system as the actor, or record only the enrollment log line | **Approved direction:** a system action in the same transaction, naming the administrator who authorized the replacement (14.4, step 14) |

**Attribution representation: approved 2026-11-12** (14.2, 14.2a):
- the columns `replaces_installation_id`, `replacement_authorized_by_user_id` and `replacement_authorized_at`;
- the all-or-none CHECK and the write-once `BEFORE UPDATE` trigger;
- the authorization, cancellation and redemption audit events, each written in the same transaction as its change.

**Outstanding decisions:** D5 and D6, before R5.

**Remaining technical issues (none blocks R1; all still outstanding):**

1. **Retention of `auth_audit_log`:** its retention policy is still undecided (`data_lifecycle_policy`: "pending an audit-retention policy"). That policy must keep replacement events, or move them somewhere else, for as long as replacement evidence is needed. The write-once columns keep the attribution meanwhile.
2. **Protection of `auth_audit_log`:** append-only for the application role, without a trigger, as for every existing administration event. Adding a trigger to the whole table is a separate security decision for Workstream B, not part of C6a.
3. **A constraint for one active key per branch:** still deferred until the production key inventory (14.5).
4. **D5 and D6:** reporting discontinuity and pilot use, decided before R5.
5. **Clean computer after a lost response:** verified in R4, not assumed (14.6).

### 14.11 R3: administration interface and the replacement feature flag

**Status:** decisions 1 and 3–6 approved 2026-11-12. **The feature flag (decision 2) was approved 2026-11-12 and is implemented as its own backend-first slice, on branch `feature/c6a-r3-replacement-admin-ui`, and passed local validation (record below). The administration interface is not part of that slice and is not implemented.**

**Why R3 needs a flag now.** R2 was merged and deployed before D6 was decided (14.4a, G superseded). Until the flag ships, a platform administrator can authorize, issue a code for and complete a replacement through the administration API, and nothing on the server stops it. Hiding the React controls would not change that, so the gate must be on the server.

**Approved decisions:**

| # | Decision |
|---|---|
| 1 | **Read model (Option A).** The installation list gains `replaces_installation_id` on each installation and a top-level `pending_replacement_id`. The pending replacement is computed by the shared definition (`replacement_eligibility.pending_replacement_at_branch`) for the primary branch, with that branch's tenant context set and no lock taken |
| 2 | **D6 exposure:** a server-enforced replacement flag, **off by default** (design below) |
| 3 | Authorization and code issuance stay **two separate, explicitly confirmed actions** |
| 4 | Confirmations are the **inline confirmations** already used on the Installations page (labelled groups, "Confirm: …" and Cancel), not modal dialogs |
| 5 | A standalone support runbook: `docs/c6a-replacement-support-runbook.md` |
| 6 | **Wording:** authorization itself does not disable the original computer; its credentials are retired only when redemption commits; collection may have a gap (14.7); support verifies the new computer before cleaning up the old one |

**Unchanged:** one-time secret handling, screen-reader behavior, the platform-administrator check and admin Origin guard, and the API's refusal codes and messages.

**Feature flag: proposed design.**

*Configuration.* One backend environment variable, `SORTVIEW_MACHINE_REPLACEMENT_ENABLED`, following the pattern of `SORTVIEW_V2_INGEST_ENABLED`:
- It is off unless the trimmed value is exactly `true`, in any case. Unset, empty or any other value is off.
- It is set in the DigitalOcean app's environment. Changing it redeploys the backend, as for every backend variable.
- **One function reads it:** `replacement_eligibility.replacement_enabled()`, on each call rather than once at import.
  - The enrollment path imports `src.services.*` and the administration path imports `services.*`, so a module-level constant would exist twice, and tests would have to patch both.
  - A per-call read keeps one rule and lets tests use `monkeypatch.setenv`.
  - The cost is one environment lookup per guarded request.
- There is no database setting and no admin toggle. The flag is an operator decision made at deploy time, not something a platform administrator can switch from the interface it restricts.

*What it guards.*

| Operation | Flag off | Where |
|---|---|---|
| Authorize a replacement | `409 replacement_disabled`, before any lock or write | `admin_installation_service.authorize_replacement`, first check |
| Issue a code for a **pending replacement** | `409 replacement_disabled`, before any write; earlier unused codes are not revoked | `issue_enrollment_code`, in the existing branch for a linked installation that owns no key |
| Redeem a replacement's code | `409 enrollment_not_ready`, internal reason `replacement_disabled`; the code is not used and nothing changes | `redeem_enrollment_code`, at the existing branch for a linked installation, before `_redeem_replacement` |
| Cancel a pending replacement | **Allowed**: it is the cleanup path | — |
| The general status change | Unchanged. Deactivating or reactivating a pending replacement still works; retiring one still answers `use_cancel_replacement` | — |
| List installations | Unchanged, plus `replacement_enabled: bool`, so the UI can hide Replace and code issuance for a pending replacement and say why | list route |
| Ordinary enrollment, ordinary codes, recovery tokens, unbound-token revocation, uploads, heartbeats | **Unaffected**: none of them reaches a guard | — |

*Redemption keeps the public refusal.* SortView Setup 1.0.14 knows only the existing public codes (14.1(c)), so a disabled redemption returns the ordinary `enrollment_not_ready`. Setup reports "not ready to be enrolled" and says the code was not used. The reason `replacement_disabled` appears only in server logs.

*Existing pending replacements while the flag is off.*
- They stay as they are and appear in the list.
- Their codes cannot be redeemed, and no new code can be issued.
- Support can cancel them.
- Turning the flag on again makes them usable, subject to every existing eligibility check.

*Changes the flag needs* (none made yet):
- **Backend:** the function and the three guards above; the `replacement_disabled` message in the administration routes; `replacement_enabled` in `InstallationsResponse`.
- **Tests, with the flag off:**
  - each guard is refused, on SQLite, and on PostgreSQL for redemption: no code used, no credential changed;
  - cancellation is still allowed;
  - ordinary enrollment, codes and recovery are unaffected.
- **Tests, value parsing:** `true`, `TRUE`, ` true `, empty, `1`, `yes` and unset.
- **Tests, existing:** R1's and R2's replacement tests turn the flag on in their fixtures and keep every assertion.
- **Deployment and docs:** a row in `docs/deployment.md`'s variable table and in `.env.example`, and the runbook's prerequisites. No migration.

*Status of the flag slice (2026-11-12).* **Implemented, and passed local validation on 2026-11-12. Not merged, not deployed. This slice does not include the R3 administration interface**, which is still to be built; until it is, replacement remains reachable only through the administration API, and only with the flag on.
- **Code:**
  - `replacement_eligibility.replacement_enabled()` and `REPLACEMENT_DISABLED`;
  - the guard first in `authorize_replacement`;
  - the guard in `issue_enrollment_code` for a linked installation that owns no key, inside its transaction, before any code is written;
  - the guard in `redeem_enrollment_code` for a linked code, inside the caller's transaction, holding only the organization `FOR SHARE` and the code `FOR UPDATE`, before any credential is touched;
  - the `replacement_disabled` message in the administration routes.
- **Tests:**
  - `tests/test_replacement_flag.py` (new): value parsing, both module identities, and authorization refused before the database is touched;
  - flag-off tests added to `test_replacement_redemption.py`, `test_admin_replacement_postgres.py` and `test_replacement_redemption_postgres.py`: each guard writes nothing; cancellation, deactivation and reactivation still work; ordinary enrollment, codes, installation creation, unbound-token revocation, recovery tokens, uploads and heartbeats are unaffected; a refused code redeems once the flag is on;
  - `replacement_disabled` added to the route refusal tests in `test_admin_replacement.py`;
  - the R1 and R2 replacement fixtures turn the flag on. No existing assertion changed.
- **Docs:** `.env.example`, `docs/deployment.md`.
- **Local validation (2026-11-12; local disposable PostgreSQL 18, never production):**
  - **Full PostgreSQL opt-in suite: 1,054 passed, 0 failed:** R2's 1,049 plus the five new flag-off PostgreSQL tests.
  - **Full Python suite without PostgreSQL: 11,342 passed, 1,054 skipped, 0 failed:** R2's 11,320 plus 22 new tests (18 in `test_replacement_flag.py`, 2 in `test_replacement_redemption.py`, 2 route refusal cases in `test_admin_replacement.py`).
  - ruff and bandit (the CI command) clean; `git diff --check` clean.
- **Not changed:** no migration, no schema change, no change to SortView Setup 1.0.14 or the Collector.
- **The support runbook** (`docs/c6a-replacement-support-runbook.md`) ships with the interface slice, not with this one.
- **Differences from the design above:**
  - `replacement_enabled` in the installation list is **deferred to the interface slice**, together with `replaces_installation_id` and `pending_replacement_id`, so the list's shape changes once. Nothing reads it before the interface exists.
  - Authorization's name is validated by the route before the service runs, so a blank or overlong name is still `422` while the flag is off. Either way nothing is written.

*Deployment order.*
- The flag ships as R3's first backend change, before the UI, because until then replacement is ungated in production.
- Before deploying it, confirm read-only that production has no replacement in progress (`collector_installations` rows with `replaces_installation_id`). The flag makes any pending replacement unusable until the flag is turned on.

**Interface workflow** (Installations page, `/libraries/:orgSlug/installations`):
1. **Replace this Collector computer.** Shown only when the flag is on and no replacement is pending, on an installation that:
   - is Contract 2, at the primary branch;
   - is Provisioning, Active or Inactive;
   - has a used code;
   - is not the pending replacement.

   The server decides eligibility; the page only avoids offering what cannot apply. An inline form takes the new computer's name and states the decision 6 wording. For a Provisioning source, it also requires the lost-response confirmation (14.3a).
2. **After `201`:** the list refreshes, a status message names the new installation, and focus moves to its card. The pending card reads "Pending replacement for Installation #X", and the source reads "Being replaced by Installation #Y".
3. **Issue enrollment code** on the pending card, as for any installation. The code appears in the existing one-time code panel, with replacement instructions.
4. **Cancel replacement** on the pending card, after an inline confirmation. It stays available while the flag is off.
5. **After redemption:** the source reads "Retired — replaced by Installation #Y", the replacement reads "Replaces Installation #X", and Cancel is no longer offered.

**Refusals:** each shows the server's message after "Nothing was changed.", keeps what was typed, and refreshes the list, so a concurrent change by another administrator is visible at once.
