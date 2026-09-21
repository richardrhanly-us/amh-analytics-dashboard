-- READ-ONLY inventory of the Admin Settings "lock" credential (organization / branch settings JSON).
--
-- SELECT only. It returns row ids, owner ids, slugs and a STATE per row -- never a password, a hash, or any part
-- of settings_json. The stored values are only COMPARED inside the server (empty? a string? a recognised hash
-- format?); they are not selected, so nothing sensitive crosses the wire or lands in a result grid, an export
-- or a screenshot. Safe to run in the Neon SQL editor. Two queries: run them one at a time.
--
-- states (identical to scripts/migrate_admin_lock_hashes.py, which is tested against this file):
--   no_lock          no password stored (an empty admin_password key is not a credential)
--   plaintext_only   security.admin_password holds a value and there is no hash   <- the migration target
--   hash_only        security.admin_password_hash holds a hash                    <- already safe
--   both             a hash AND a stale plaintext value                            <- drop the plaintext key
--   unexpected_shape the JSON is not what the application writes: needs a human, never auto-migrated
--
-- Tables: organization_settings (one row per organization, written by the Admin Settings page) and
-- branch_settings (never written with a `security` block by current code; any non-`no_lock` row there needs review).

-- @@ detail @@
WITH scoped AS (
    SELECT 'organization_settings' AS table_name, os.id AS settings_row_id, os.organization_id AS owner_id,
           o.slug AS owner_slug, os.settings_json AS j
    FROM organization_settings os
    JOIN organizations o ON o.id = os.organization_id
    UNION ALL
    SELECT 'branch_settings', bs.id, bs.branch_id, b.slug, bs.settings_json
    FROM branch_settings bs
    JOIN branches b ON b.id = bs.branch_id
), probed AS (
    SELECT table_name, settings_row_id, owner_id, owner_slug,
           jsonb_typeof(j)                                     AS json_type,
           jsonb_typeof(j -> 'security')                       AS security_type,
           jsonb_typeof(j #> '{security,admin_password}')      AS legacy_type,
           jsonb_typeof(j #> '{security,admin_password_hash}') AS hash_type,
           coalesce(j #>> '{security,admin_password}', '') <> ''      AS legacy_nonempty,
           coalesce(j #>> '{security,admin_password_hash}', '') <> '' AS hash_nonempty,
           coalesce(j #>> '{security,admin_password_hash}', '') ~ '^(scrypt|pbkdf2):' AS hash_format_ok
    FROM scoped
), classified AS (
    SELECT *,
           CASE
               WHEN json_type IS DISTINCT FROM 'object' THEN 'unexpected_shape'
               WHEN security_type IS NOT NULL AND security_type <> 'object' THEN 'unexpected_shape'
               WHEN legacy_type IS NOT NULL AND legacy_type NOT IN ('string', 'null') THEN 'unexpected_shape'
               WHEN hash_type IS NOT NULL AND hash_type NOT IN ('string', 'null') THEN 'unexpected_shape'
               WHEN hash_nonempty AND NOT hash_format_ok THEN 'unexpected_shape'
               WHEN legacy_nonempty AND hash_nonempty THEN 'both'
               WHEN legacy_nonempty THEN 'plaintext_only'
               WHEN hash_nonempty THEN 'hash_only'
               ELSE 'no_lock'
           END AS state
    FROM probed
)
SELECT table_name, settings_row_id, owner_id, owner_slug, state,
       (legacy_type = 'string' AND NOT legacy_nonempty) AS empty_legacy_key_present
FROM classified
ORDER BY table_name, settings_row_id;

-- @@ summary @@
WITH scoped AS (
    SELECT 'organization_settings' AS table_name, os.id AS settings_row_id, os.settings_json AS j
    FROM organization_settings os
    UNION ALL
    SELECT 'branch_settings', bs.id, bs.settings_json
    FROM branch_settings bs
), probed AS (
    SELECT table_name, settings_row_id,
           jsonb_typeof(j)                                     AS json_type,
           jsonb_typeof(j -> 'security')                       AS security_type,
           jsonb_typeof(j #> '{security,admin_password}')      AS legacy_type,
           jsonb_typeof(j #> '{security,admin_password_hash}') AS hash_type,
           coalesce(j #>> '{security,admin_password}', '') <> ''      AS legacy_nonempty,
           coalesce(j #>> '{security,admin_password_hash}', '') <> '' AS hash_nonempty,
           coalesce(j #>> '{security,admin_password_hash}', '') ~ '^(scrypt|pbkdf2):' AS hash_format_ok
    FROM scoped
), classified AS (
    SELECT *,
           CASE
               WHEN json_type IS DISTINCT FROM 'object' THEN 'unexpected_shape'
               WHEN security_type IS NOT NULL AND security_type <> 'object' THEN 'unexpected_shape'
               WHEN legacy_type IS NOT NULL AND legacy_type NOT IN ('string', 'null') THEN 'unexpected_shape'
               WHEN hash_type IS NOT NULL AND hash_type NOT IN ('string', 'null') THEN 'unexpected_shape'
               WHEN hash_nonempty AND NOT hash_format_ok THEN 'unexpected_shape'
               WHEN legacy_nonempty AND hash_nonempty THEN 'both'
               WHEN legacy_nonempty THEN 'plaintext_only'
               WHEN hash_nonempty THEN 'hash_only'
               ELSE 'no_lock'
           END AS state
    FROM probed
)
SELECT table_name, state, count(*) AS row_count
FROM classified
GROUP BY table_name, state
ORDER BY table_name, state;
