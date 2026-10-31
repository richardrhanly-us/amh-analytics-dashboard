# C6a — Contract v2 Replacement-Machine Design

**Status:** Proposed — design review required  

**Scope:** Support-controlled replacement of an enrolled Contract v2 Collector computer  

**Implementation status:** Not implemented

## 1. Objective

Allow a platform administrator to replace a failed, lost, or retired Collector computer without losing existing branch analytics, creating competing active Collectors, or weakening Contract v2 security controls.

This workflow must support a new Collector installation on a clean Windows computer while preserving the existing customer, branch, historical event records, and original Contract v2 cutover.

## 2. Existing behavior

- Collector 1.0.14 supports guided Contract v2 enrollment on a clean machine.
- Installations support Provisioning, Active, Inactive, and Retired states.
- Agent tokens are associated with installations through `agent_tokens.installation_id`.
- Ingest keys are currently associated with a customer and branch, not a specific installation.
- Ordinary Contract v2 enrollment is blocked when a branch has an active ingest key or any previous v2 cutover.
- Both enrollment-code issuance and enrollment redemption enforce these restrictions.
- The server stores no Collector HMAC secret; the secret is generated and protected locally.
- Fresh Collector bootstrap starts at the end of each source file.

These existing safeguards must remain intact for ordinary enrollment.

## 3. Proposed replacement identity

Each replacement computer receives a new `collector_installations` record.

The replacement installation retains the original operational customer and branch associations but has its own installation ID, enrollment code, agent token, ingest key ID, and locally generated HMAC secret.

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

## 5. Replacement authorization

Replacement is an explicit platform-administrator operation, separate from ordinary installation creation and enrollment.

The administrator must identify the existing installation and confirm replacement.

The server must verify:

- The library and branch are operational and eligible.
- The selected installation belongs to the specified library and branch.
- The installation uses Contract v2.
- The ingest key being retired belongs to the expected customer, branch, and installation.
- The requested replacement does not conflict with another active or pending replacement.
- The authorization cannot be reused for another installation or branch.

An ordinary enrollment code must never act as replacement authorization by itself.

## 6. Server-side replacement workflow

Replacement is a two-phase operation.

****Phase A — Authorization****

A platform administrator authorizes replacement of an existing Contract v2 installation.

The server creates a new Provisioning installation explicitly linked to the installation it replaces. It records the authorization and permits a short-lived, one-time replacement enrollment code.

The original installation, agent tokens, and ingest key remain operational during this phase.

Authorization must reject concurrent pending replacements, invalid tenant relationships, and ambiguous key ownership. A compromised old machine must be deactivated immediately through an explicit security response rather than left operating until redemption.

An abandoned replacement may be cancelled without interrupting the original Collector.

****Phase B — Enrollment redemption and credential cutover****

Successful redemption must atomically:

1. Validate the enrollment code and replacement authorization.
2. Serialize the operation against competing branch enrollment or replacement requests.
3. Verify the original installation, ingest-key ownership, effective v2 cutover, and applicable token restrictions.
4. Transition a Provisioning, Active, or Inactive source installation to Retired; leave an already Retired source unchanged after verifying its eligibility and ownership.
5. Invalidate the old installation's agent tokens.
6. Retire the verified old ingest key.
7. Issue a new installation-bound agent token and ingest key.
8. Mark the one-time code as consumed.
9. Preserve the original v2 cutover and historical events.
10. Record the credential transition in the audit trail.

Any failed database operation must roll back the complete cutover.

Both `/v2/upload` and `/v2/status` must enforce token-to-ingest-key installation ownership. Active unbound tokens must be resolved explicitly before replacement; they cannot silently bypass installation isolation.

Replacement-specific enrollment refusals must remain compatible with the existing Collector 1.0.14 installer.

### Additional redemption and recovery rules

The normal replacement cutover retires the old installation and invalidates its bound credentials.

A Provisioning, Active, or Inactive source transitions to Retired. An already Retired source remains unchanged. All sources require identity, historical key ownership, and replacement-eligibility verification.

If the branch has no active ingest key, replacement may proceed only through an explicitly authorized recovery path that verifies the source installation's ownership of its most recent key and confirms that no competing active key or Collector exists. Otherwise the operation must fail closed and require operator reconciliation.

The replacement transaction must verify the result of each required key-retirement operation. An unexpected zero-row update is a failure, not a successful revocation.

Any token or key ownership migration must preserve the behavior of existing NULL-owner keys until reconciliation is complete.

### Source installation lifecycle clarification

During successful replacement redemption, a Provisioning, Active, or Inactive source installation transitions to Retired.

An already Retired source remains Retired. Its identity, historical key ownership, and replacement eligibility must still be verified.

No replacement operation may leave the superseded source installation eligible to return to Provisioning.

## 7. Replacement enrollment

The new machine must use the existing guided installer where practical.

The enrollment service must distinguish authorized replacement enrollment from ordinary enrollment and enforce the distinction independently of the administration interface.

For an authorized replacement, redemption should:

- Validate the replacement authorization and its installation binding.
- Preserve the branch's original v2 cutover.
- Issue a new agent token and ingest key.
- Generate no server-side HMAC secret.
- Return the existing Contract v2 configuration and classification rules.
- Ensure replacement authorization cannot be redeemed multiple times.

The replacement machine generates and protects its own HMAC secret.

## 8. Concurrency and security

The workflow must prevent:

- Two simultaneous replacements for one branch.
- Multiple valid Collectors operating under competing installations.
- Reuse of the old installation's revoked credentials.
- Reuse of retired ingest keys.
- Cross-library or cross-branch replacement.
- Unauthorized enrollment through ordinary provisioning routes.
- Partial database changes following an operation failure.

Branch-scoped locking and transactional checks must cover both replacement authorization and enrollment redemption.

The final implementation must define how abandoned replacement authorizations are cancelled or resumed.

## 9. Cursor, reporting continuity, and historical integrity

A replacement machine creates a new machine-local HMAC secret and therefore a new event and item identity namespace.

A fresh bootstrap at the end of the source files can create permanent ingestion gaps, including events generated during an outage or between credential cutover and bootstrap.

The changed HMAC identity namespace may also prevent item-level matching across the replacement boundary. Existing hold de-duplication, hold retractions, and classification corrections require investigation.

****These risks are not approved for general commercial release.****

Before C6a implementation is approved, determine:

1. Which source events can be lost during planned and unplanned replacement.
2. Whether a bounded, privacy-preserving replay or handoff mechanism is feasible.
3. How event duplication is prevented when old and new HMAC identities differ.
4. How historical holds and later retractions behave across a key transition.
5. Whether the dashboard and reports must identify a replacement boundary, ingestion gap, or reduced-confidence reporting period.
6. How replacement monitoring detects an installation that redeems its code but never begins successful ingestion.

If lossless continuity cannot be guaranteed, the limitations and a tested mitigation must be explicitly reviewed before commercialization.

Do not treat an undocumented permanent reporting gap as acceptable behavior.

### Confirmed continuity findings — October 31, 2026

Code inspection established the following:

1. **Fresh bootstrap starts at EOF.** `collector/bootstrap_state.py::bootstrap_v2_state` initializes source cursors from `reader.read_new_lines(path, None)` and refuses existing v2 or legacy state. Collector 1.0.14 has no supported cursor-handoff or bounded-replay initialization mode.

2. **Item identities change with the master secret.** `collector/v2_identity.py` derives `K_item` from the machine-local master secret. A replacement generating a new secret produces different `item_key` values for the same barcode.

3. **Cross-key hold retractions cannot match.** `src/metrics_v2.py::build_acs_item_summary_v2` keeps the latest record per `item_key`. A hold under the old identity and a later `non_hold_101` record under the new identity are treated as separate items.

4. **Non-hold events do not require an existing local hold.** `collector/v2_transform.py` emits non-hold ACS events even when `cache.hold_get(item_key)` returns no row. The missing retraction is therefore a cloud identity-correlation issue, not simply an omitted event.

5. **Historical classification corrections require local state.** `_corrections()` uses the patron cache and hold ledger to generate updated hold events. A replacement with an empty ledger cannot generate corrections for holds known only to the previous installation.

6. **Copying local SQLite state is insufficient.** `collector/v2_patrons.py` stores HMAC-derived patron and item identifiers. Those identifiers do not match a replacement's newly derived subkeys.

7. **The existing master secret is machine- and key-bound.** `collector/v2_keys.py` uses Windows DPAPI machine-scope protection and refuses loading a secret whose stored `key_id` differs from the configured key. There is no supported cross-machine secret recovery or transfer.

**Conclusion:** Cursor-only migration cannot provide complete reporting continuity. The approved C6a implementation design must address event recovery, cross-key item correlation, and local classification state together.

These are findings from code inspection, not executed replacement-specific tests. No mitigation has yet been selected or approved.

A new Collector release is likely necessary for cursor handoff or bounded replay, but its exact scope remains undetermined.

### Additional ingestion-continuity findings

Collector v2 uses the Tech Logic source files as its durable source of undelivered records. It advances its read cursors only after successful delivery. There is no separate persistent upload outbox.

Therefore, undelivered records may remain available beyond the previous Collector's committed cursors.

Investigate:

- Planned replacement using a verified handoff of the old Collector's committed cursors.
- Failed-machine recovery using a bounded replay start derived from available server-side event history.
- Timestamp ties and other ambiguities at a proposed replay boundary.
- File rotation, truncation, replacement, and source-file identity changes.
- Source files or cursor state lost with the original computer.
- Duplicate detection across different HMAC identity namespaces.

**Collector and installer compatibility:** Collector 1.0.14 supports the existing fresh-enrollment response, but its clean-machine check refuses prior state and its bootstrap refuses an existing state file and seeds only at end-of-file. A cursor handoff or bounded replay may require a new Collector release and installer/bootstrap changes. Evaluate these costs and the compatibility of deployed 1.0.14 Collectors before selecting a continuity mitigation. The requirement to preserve 1.0.14 enrollment refusal behavior does not imply continuity can be delivered server-only.

**Local classification-state continuity:** Investigate the local patron cache, hold ledger, and HMAC-derived patron and item identifiers. Cursor transfer alone does not transfer those states. A replacement machine with a new secret may not be able to use copied local cache data; historical holds, classifications, and later corrections or retractions may behave differently. Assess privacy-safe transfer or reconstruction options and prove reporting correctness across the key boundary rather than assuming cursor handoff solves hold continuity.

No replay or cursor-handoff method is approved until the investigation demonstrates its safety.

### Approval gates

****Before C6a implementation approval:****

The investigation must establish the actual ingestion-gap and cross-key hold-integrity failure modes, identify feasible mitigation approaches, and produce an approved implementation design.

This includes investigating replay and cursor handoff, historical hold/retraction behavior, reporting-gap visibility, and monitoring of incomplete replacements.

****Before commercial release:****

The selected mitigation must be implemented and tested. Remaining limitations must be explicitly documented and approved; permanent ingestion gaps or hold-count inconsistencies cannot be silently accepted.

## 10. Failure recovery

Replacement authorization and enrollment redemption are distinct phases.

****Failures before redemption:****

- The existing Collector and credentials remain operational.
- An abandoned replacement can be cancelled.
- An expired enrollment code can be reissued under the same valid pending replacement authorization.
- A compromised source machine is handled through immediate deactivation rather than normal planned-replacement continuity.

****Failures after successful redemption:****

- The old installation remains Retired, and its previous credentials remain invalid.
- The replacement installer may resume when its local setup state is intact.
- An enrollment response lost after server-side success may leave the replacement computer without the token, key ID, classification rules, and other enrollment configuration. An installation-bound recovery token alone cannot repair that state. The proposed recovery is another explicitly authorized replacement from the unconfirmed installation, subject to validation of the replacement chain, ownership, one-time-code rules, and branch key restrictions. Do not retry a consumed code as if it were unused.
- A failed replacement machine may itself require another explicitly authorized replacement.
- Recovery must not recreate the original cutover, reactivate old credentials, or issue competing active ingest keys.

All recovery actions must be auditable.

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
- Fresh ingestion resumes under the original customer and branch.
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
- Failure before redemption leaves the original Collector unaffected.
- Failure after redemption does not restore old credentials or produce a second active key.
- The approved data-integrity mitigation is tested before commercial release.

### Monitoring acceptance criteria clarification

Replacement monitoring must identify both:

- A replacement that never sends an accepted heartbeat.
- A replacement that sends healthy heartbeats but fails to ingest records despite evidence of expected sorter activity.

A clean heartbeat or successful Collector run is not, by itself, evidence that the configured source files are correct.

Initially evaluate a server-side comparison of received events against validated site activity patterns. Any resulting alert must be documented as a heuristic and evaluated for false positives during legitimate inactivity.

Additional typed heartbeat diagnostics may be considered if existing server-side evidence is insufficient. Such changes require separate API and Collector compatibility planning.

## 12. Scope boundary

General-purpose key rotation, multi-branch provisioning, and changes to legacy Contract v1 enrollment remain outside C6a.

Automatic cursor migration, bounded replay, historical-data correction, or cross-key hold reconciliation are not automatically included or excluded. Their inclusion depends on the Section 9 investigation and approval of a concrete mitigation design.

No historical event records may be rewritten merely to make a replacement appear successful.

Self-service customer replacement without platform-administrator approval remains outside C6a.

## 13. Decisions and open questions

****Approved October 31, 2026****

- A replacement creates a new installation identity.
- The original installation becomes Retired following successful replacement redemption.
- Credential cutover occurs at enrollment redemption, not administrator authorization.
- The original branch cutover and historical event records are preserved.
- Ingestion gaps and cross-key hold inconsistencies require investigation and mitigation-design approval before implementation.
- The selected mitigation must be validated before commercial release.

****Still requiring design approval****

1. Exact schema representation of replacement authorization and installation-to-key ownership.
2. Legacy NULL-owner key reconciliation for both active and retired keys, and recovery of branches without active keys.
3. Database constraints guaranteeing a single active ingest identity and a single pending replacement.
4. Replacement authorization expiry, cancellation, and recovery semantics.
5. Consistent transaction locking order and concurrency guarantees.
6. Cursor continuity, replay feasibility, and cross-key hold reconciliation.
7. Reporting and monitoring behavior during replacement and inactivity.
8. Final administration API, UI, and operational runbook integration.

****Legacy key reconciliation clarification****

Legacy key reconciliation must cover both active and retired ingest keys whose installation ownership is not recorded.

The procedure must verify ownership evidence before backfilling, including any installation-bound tokens and relevant enrollment records. A retired key must not automatically be attributed to an installation merely because it is the most recent key for the branch.

Ambiguous cases require explicit operator reconciliation and must fail closed until resolved.

No schema migration, endpoint, or production change should be made until these decisions have been reviewed.
