-- C6a R4, stage 0: read-only baseline of the qualification library before any replacement.
--
-- Run with psql (docs/c6a-r4-qualification.md, stage 0), for example:
--   psql -h <host> -U <read-only role> -d <database> -W -v slug=sortview-g2-qualification-2 -v installation=9 -f scripts/c6a_r4/stage0_baseline.sql
--
-- Everything runs inside one READ ONLY transaction that is rolled back. Only SELECTs and set_config(..., true), which
-- is local to this transaction. Nothing secret is selected: no token or code hash, no hostname, no Collector secret.
-- Key ids are shown as an 8-character prefix only.
--
-- Row-level security: ingest_key_ids and the event tables are filtered by app.operational_customer_id and
-- app.operational_branch_id. This script sets both to the library's primary branch, so it reads the same rows as the
-- application, whichever role runs it.

\set ON_ERROR_STOP on
\if :{?slug}
\else
  \echo 'Pass -v slug=<library slug>'
  \quit
\endif
\if :{?installation}
\else
  \echo 'Pass -v installation=<source installation id>'
  \quit
\endif

BEGIN TRANSACTION READ ONLY;

\echo
\echo '== 1. Library and primary branch (expect exactly one row; operational_branch_id = branch_id)'
SELECT o.id AS organization_id, o.slug, o.status, o.operational_customer_id,
       b.id AS branch_id, b.slug AS branch_slug, b.status AS branch_status, b.operational_branch_id
FROM organizations o
JOIN branches b ON b.organization_id = o.id AND b.is_primary
WHERE o.slug = :'slug';

-- Fails (and stops the script) unless there is exactly one primary branch.
SELECT o.operational_customer_id AS customer_id, b.id AS branch_id
FROM organizations o
JOIN branches b ON b.organization_id = o.id AND b.is_primary
WHERE o.slug = :'slug'
\gset

SELECT set_config('app.operational_customer_id', :'customer_id', true) AS tenant_customer,
       set_config('app.operational_branch_id', :'branch_id', true) AS tenant_branch;

\echo
\echo '== 2. Installations in the library'
SELECT ci.id, ci.name, ci.status, ci.required_contract, ci.branch_id,
       ci.installed_at, ci.last_seen_at, date_trunc('second', now() - ci.last_seen_at) AS seen_ago,
       ci.replaces_installation_id, ci.replacement_authorized_at
FROM collector_installations ci
JOIN organizations o ON o.id = ci.organization_id
WHERE o.slug = :'slug'
ORDER BY ci.id;

\echo
\echo '== 3. Ingest keys at the branch'
SELECT k.id, left(k.key_id, 8) AS key_id_prefix, k.installation_id, k.status, k.created_at, k.retired_at,
       k.last_heartbeat_at, k.last_success_at, k.health_status, k.pending_outbox_count, k.quarantined_count,
       k.collector_last_run_at, k.collector_schedule_status
FROM ingest_key_ids k
WHERE k.customer_id = :customer_id AND k.branch_id = :branch_id
ORDER BY k.id;

\echo
\echo '== 4. Agent tokens at the branch (installation_id NULL = legacy unbound)'
SELECT t.id, t.installation_id, t.is_active, t.created_at, t.last_used_at
FROM agent_tokens t
WHERE t.customer_id = :customer_id AND t.branch_id = :branch_id
ORDER BY t.id;

\echo
\echo '== 5. Contract v2 cutover at the branch (record it; a replacement must never change it)'
SELECT c.id, c.cutover_at, c.set_by, c.set_at
FROM v2_cutovers c
WHERE c.customer_id = :customer_id AND c.branch_id = :branch_id
ORDER BY c.id;

\echo
\echo '== 6. Events by table, state and key (rows_fingerprint must not change for a retired key)'
SELECT 'acs_item_events' AS event_table, e.state, left(e.key_id, 8) AS key_id_prefix,
       count(*) AS events, min(e.event_time) AS first_event, max(e.event_time) AS last_event,
       md5(string_agg(e.id::text || ':' || e.event_key, ',' ORDER BY e.id)) AS rows_fingerprint
FROM acs_item_events e
WHERE e.customer_id = :customer_id AND e.branch_id = :branch_id
GROUP BY e.state, e.key_id
UNION ALL
SELECT 'checkin_events', NULL, left(e.key_id, 8), count(*), min(e.event_time), max(e.event_time),
       md5(string_agg(e.id::text || ':' || e.event_key, ',' ORDER BY e.id))
FROM checkin_events e
WHERE e.customer_id = :customer_id AND e.branch_id = :branch_id
GROUP BY e.key_id
UNION ALL
SELECT 'reject_events', NULL, left(e.key_id, 8), count(*), min(e.event_time), max(e.event_time),
       md5(string_agg(e.id::text || ':' || e.event_key, ',' ORDER BY e.id))
FROM reject_events e
WHERE e.customer_id = :customer_id AND e.branch_id = :branch_id
GROUP BY e.key_id
ORDER BY 1, 2, 3;

\echo
\echo '== 7. Enrollment codes per installation (counts only)'
SELECT c.installation_id,
       count(*) FILTER (WHERE c.used_at IS NOT NULL) AS used,
       count(*) FILTER (WHERE c.used_at IS NULL AND c.revoked_at IS NULL AND c.expires_at > now()) AS unused_live,
       count(*) FILTER (WHERE c.used_at IS NULL AND c.revoked_at IS NULL AND c.expires_at <= now()) AS expired,
       count(*) FILTER (WHERE c.used_at IS NULL AND c.revoked_at IS NOT NULL) AS revoked
FROM collector_enrollment_codes c
JOIN collector_installations ci ON ci.id = c.installation_id
JOIN organizations o ON o.id = ci.organization_id
WHERE o.slug = :'slug'
GROUP BY c.installation_id
ORDER BY c.installation_id;

\echo
\echo '== 8. Latest audit events for the library'
SELECT a.id, a.created_at, a.event_type, a.is_success,
       a.metadata ->> 'installation_id' AS installation_id,
       a.metadata ->> 'replaces_installation_id' AS replaces_installation_id
FROM auth_audit_log a
WHERE a.metadata ->> 'target_org_slug' = :'slug'
ORDER BY a.id DESC
LIMIT 25;

\echo
\echo '== 9. Stage 0 checks (every column must be t)'
SELECT
    (SELECT count(*) = 1 AND bool_and(ci.status = 'active' AND ci.required_contract = 2)
     FROM collector_installations ci JOIN organizations o ON o.id = ci.organization_id
     WHERE o.slug = :'slug' AND ci.id = :installation AND ci.branch_id = :branch_id)            AS source_active_contract2_here,
    (SELECT coalesce(bool_and(ci.last_seen_at > now() - interval '30 minutes'), false)
     FROM collector_installations ci WHERE ci.id = :installation)                                AS source_seen_in_30_min,
    (SELECT count(*) = 1 AND bool_and(k.installation_id = :installation)
     FROM ingest_key_ids k
     WHERE k.customer_id = :customer_id AND k.branch_id = :branch_id AND k.status = 'active')    AS one_active_key_owned_by_source,
    (SELECT count(*) = 0
     FROM agent_tokens t
     WHERE t.customer_id = :customer_id AND t.branch_id = :branch_id
       AND t.installation_id IS NULL AND t.is_active)                                           AS no_active_unbound_tokens,
    (SELECT count(*) >= 1
     FROM agent_tokens t WHERE t.installation_id = :installation AND t.is_active)               AS source_has_active_token,
    (SELECT count(*) = 1
     FROM v2_cutovers c WHERE c.customer_id = :customer_id AND c.branch_id = :branch_id)        AS one_cutover,
    (SELECT count(*) = 0
     FROM collector_installations ci JOIN organizations o ON o.id = ci.organization_id
     WHERE o.slug = :'slug' AND ci.replaces_installation_id IS NOT NULL)                        AS no_replacement_yet;

ROLLBACK;
