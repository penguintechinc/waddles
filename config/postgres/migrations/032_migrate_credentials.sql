-- =============================================================================
-- Migration 032: Migrate existing credentials to unified platform_integrations
-- =============================================================================
-- Purpose: Consolidate platform credentials from legacy tables into the new
--          unified platform_integrations table for centralized credential
--          management and encryption
--
-- This migration:
-- 1. Migrates bot credentials from platform_configs
-- 2. Migrates community OAuth tokens from music_oauth_tokens
-- 3. Validates data integrity across both sources
-- 4. Keeps original tables intact for rollback safety
--
-- Duration: Expected < 5 seconds for typical deployments
-- Risk Level: LOW (read-only source, insert-only target with ON CONFLICT)
-- =============================================================================

-- Fresh-DB replay: 000_create_base_schema creates platform_configs in its newer
-- key/value shape (config_key/config_value), which has none of the legacy credential
-- columns (access_token, ...) this file reads, so it always failed on an empty DB --
-- where there is also nothing to migrate. The whole migration is therefore gated on the
-- legacy column being present; it runs unchanged against any DB that really has the
-- legacy table shape. (Already-migrated DBs have this file recorded as applied.)
DO $outer$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'platform_configs'
                   AND column_name = 'access_token') THEN
    RAISE NOTICE '032_migrate_credentials: legacy platform_configs shape absent (fresh DB); nothing to migrate';
    RETURN;
  END IF;

  EXECUTE $body$

-- =============================================================================
-- PART 1: Pre-Migration Validation
-- =============================================================================
-- Capture baseline counts for verification

DO $$
DECLARE
    platform_configs_count INT;
    music_oauth_tokens_count INT;
BEGIN
    SELECT COUNT(*) INTO platform_configs_count FROM platform_configs;
    SELECT COUNT(*) INTO music_oauth_tokens_count FROM music_oauth_tokens;

    RAISE NOTICE '=== PRE-MIGRATION COUNTS ===';
    RAISE NOTICE 'platform_configs: %', platform_configs_count;
    RAISE NOTICE 'music_oauth_tokens: %', music_oauth_tokens_count;
    RAISE NOTICE 'Total credentials to migrate: %', (platform_configs_count + music_oauth_tokens_count);
END $$;


-- =============================================================================
-- PART 2: Migrate Bot Credentials from platform_configs
-- =============================================================================
-- These represent bot-level platform authentications (Discord, Twitch, etc.)
-- No community_id or user_id association
-- integration_type = 'bot'

INSERT INTO platform_integrations (
    platform,
    integration_type,
    community_id,
    user_id,
    access_token,
    refresh_token,
    client_id,
    client_secret,
    token_type,
    expires_at,
    scopes,
    config_data,
    is_active,
    is_encrypted,
    created_at,
    updated_at
)
SELECT
    platform,
    'bot'::VARCHAR(20),
    NULL,
    NULL,
    access_token,
    refresh_token,
    client_id,
    client_secret,
    token_type,
    expires_at,
    scopes,
    config_data,
    is_active,
    is_encrypted,
    COALESCE(created_at, CURRENT_TIMESTAMP),
    COALESCE(updated_at, CURRENT_TIMESTAMP)
FROM platform_configs
WHERE platform IS NOT NULL
    AND access_token IS NOT NULL
ON CONFLICT (platform, integration_type, community_id, user_id, client_id)
DO NOTHING;

DO $$
DECLARE
    rows_inserted INT;
BEGIN
    SELECT COUNT(*) INTO rows_inserted
    FROM platform_integrations
    WHERE integration_type = 'bot';

    RAISE NOTICE '✓ Migrated bot credentials: % rows', rows_inserted;
END $$;


-- =============================================================================
-- PART 3: Migrate Community OAuth Tokens from music_oauth_tokens
-- =============================================================================
-- These represent community-level OAuth integrations (Spotify, Apple Music, etc.)
-- Associated with community_id
-- integration_type = 'community_oauth'

INSERT INTO platform_integrations (
    platform,
    integration_type,
    community_id,
    user_id,
    access_token,
    refresh_token,
    client_id,
    client_secret,
    token_type,
    expires_at,
    scopes,
    config_data,
    is_active,
    is_encrypted,
    created_at,
    updated_at
)
SELECT
    platform,
    'community_oauth'::VARCHAR(20),
    community_id,
    NULL,
    access_token,
    refresh_token,
    client_id,
    client_secret,
    token_type,
    expires_at,
    scopes,
    config_data,
    is_active,
    is_encrypted,
    COALESCE(created_at, CURRENT_TIMESTAMP),
    COALESCE(updated_at, CURRENT_TIMESTAMP)
FROM music_oauth_tokens
WHERE platform IS NOT NULL
    AND access_token IS NOT NULL
    AND community_id IS NOT NULL
ON CONFLICT (platform, integration_type, community_id, user_id, client_id)
DO NOTHING;

DO $$
DECLARE
    rows_inserted INT;
BEGIN
    SELECT COUNT(*) INTO rows_inserted
    FROM platform_integrations
    WHERE integration_type = 'community_oauth';

    RAISE NOTICE '✓ Migrated community OAuth tokens: % rows', rows_inserted;
END $$;


-- =============================================================================
-- PART 4: Data Integrity Validation
-- =============================================================================

DO $$
DECLARE
    total_integrations INT;
    bot_count INT;
    community_oauth_count INT;
    duplicate_count INT;
BEGIN
    -- Total count
    SELECT COUNT(*) INTO total_integrations FROM platform_integrations;
    RAISE NOTICE '';
    RAISE NOTICE '=== POST-MIGRATION COUNTS ===';
    RAISE NOTICE 'Total platform_integrations: %', total_integrations;

    -- Count by type
    SELECT COUNT(*) INTO bot_count
    FROM platform_integrations WHERE integration_type = 'bot';
    RAISE NOTICE 'Bot integrations: %', bot_count;

    SELECT COUNT(*) INTO community_oauth_count
    FROM platform_integrations WHERE integration_type = 'community_oauth';
    RAISE NOTICE 'Community OAuth integrations: %', community_oauth_count;

    -- Check for duplicates (should be zero)
    SELECT COUNT(*) INTO duplicate_count
    FROM (
        SELECT platform, integration_type, community_id, user_id, client_id
        FROM platform_integrations
        GROUP BY platform, integration_type, community_id, user_id, client_id
        HAVING COUNT(*) > 1
    ) duplicates;

    IF duplicate_count > 0 THEN
        RAISE WARNING 'Found % duplicate credentials - likely previous run', duplicate_count;
    ELSE
        RAISE NOTICE '✓ No duplicate credentials detected';
    END IF;

    -- Verify critical fields
    IF EXISTS (
        SELECT 1 FROM platform_integrations
        WHERE access_token IS NULL AND is_active = TRUE
    ) THEN
        RAISE WARNING 'Found active credentials with NULL access_token';
    ELSE
        RAISE NOTICE '✓ All active credentials have access_token';
    END IF;

    IF EXISTS (
        SELECT 1 FROM platform_integrations
        WHERE platform IS NULL
    ) THEN
        RAISE WARNING 'Found records with NULL platform';
    ELSE
        RAISE NOTICE '✓ All records have platform set';
    END IF;
END $$;


-- =============================================================================
-- PART 5: Distribution Summary
-- =============================================================================

DO $$
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== PLATFORM DISTRIBUTION (Active) ===';

    RETURN QUERY
    SELECT
        platform,
        COUNT(*) AS count
    FROM platform_integrations
    WHERE is_active = TRUE
    GROUP BY platform
    ORDER BY count DESC;
END $$;

-- Run the distribution query
SELECT
    platform,
    COUNT(*) AS count
FROM platform_integrations
WHERE is_active = TRUE
GROUP BY platform
ORDER BY count DESC;


-- =============================================================================
-- PART 6: Integration Type Distribution
-- =============================================================================

DO $$
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== INTEGRATION TYPE DISTRIBUTION ===';

    RETURN QUERY
    SELECT
        integration_type,
        COUNT(*) AS count,
        COUNT(*) FILTER (WHERE is_active = TRUE) AS active_count
    FROM platform_integrations
    GROUP BY integration_type
    ORDER BY count DESC;
END $$;

-- Run the type distribution query
SELECT
    integration_type,
    COUNT(*) AS count,
    COUNT(*) FILTER (WHERE is_active = TRUE) AS active_count
FROM platform_integrations
GROUP BY integration_type
ORDER BY count DESC;


-- =============================================================================
-- PART 7: Encryption Status Check
-- =============================================================================

DO $$
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== ENCRYPTION STATUS ===';

    RETURN QUERY
    SELECT
        is_encrypted,
        COUNT(*) AS count
    FROM platform_integrations
    GROUP BY is_encrypted
    ORDER BY is_encrypted;
END $$;

-- Run the encryption status query
SELECT
    is_encrypted,
    COUNT(*) AS count
FROM platform_integrations
GROUP BY is_encrypted
ORDER BY is_encrypted;


-- =============================================================================
-- PART 8: Duplicate Detection (for operational awareness)
-- =============================================================================

DO $$
BEGIN
    RAISE NOTICE '';
    RAISE NOTICE '=== CHECKING FOR DUPLICATE CREDENTIALS ===';

    IF EXISTS (
        SELECT 1
        FROM platform_integrations
        GROUP BY platform, integration_type, community_id, user_id, client_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE NOTICE 'Duplicates found (showing first 10):';
        RETURN QUERY
        SELECT
            platform,
            integration_type,
            community_id,
            user_id,
            COUNT(*) AS duplicate_count
        FROM platform_integrations
        GROUP BY platform, integration_type, community_id, user_id
        HAVING COUNT(*) > 1
        LIMIT 10;
    ELSE
        RAISE NOTICE '✓ No duplicates detected';
    END IF;
END $$;


-- =============================================================================
-- PART 9: Rollback Safety Notes
-- =============================================================================
-- Original source tables are preserved for rollback safety
-- DO NOT execute the DROP TABLE statements below unless:
--   1. All services have been running successfully for at least 2 weeks
--   2. Monitoring confirms no issues with credential access
--   3. A full backup has been created
--   4. Management approval is obtained
--
-- To rollback: Restore from backup or delete from platform_integrations
-- using the original table data as reference
-- =============================================================================

-- DROP TABLE platform_configs;  -- COMMENTED OUT - Keep for rollback
-- DROP TABLE music_oauth_tokens;  -- COMMENTED OUT - Keep for rollback


-- =============================================================================
-- PART 10: Final Summary
-- =============================================================================

DO $$
DECLARE
    total_count INT;
BEGIN
    SELECT COUNT(*) INTO total_count FROM platform_integrations;

    RAISE NOTICE '';
    RAISE NOTICE '========================================';
    RAISE NOTICE 'MIGRATION 032 COMPLETE';
    RAISE NOTICE '========================================';
    RAISE NOTICE 'Total credentials in platform_integrations: %', total_count;
    RAISE NOTICE 'Original tables: PRESERVED (rollback-safe)';
    RAISE NOTICE 'Status: READY FOR PRODUCTION';
    RAISE NOTICE '========================================';
    RAISE NOTICE '';
END $$;




-- =============================================================================
-- MANUAL VERIFICATION QUERIES (run separately if needed)
-- =============================================================================
-- Uncomment and run these queries to verify migration success:

/*

-- Verify total count
SELECT
    'platform_integrations' AS table_name,
    COUNT(*) AS total_rows
FROM platform_integrations;

-- Verify distribution by type
SELECT
    integration_type,
    COUNT(*) AS count,
    COUNT(*) FILTER (WHERE is_active = TRUE) AS active_count,
    COUNT(*) FILTER (WHERE is_encrypted = TRUE) AS encrypted_count
FROM platform_integrations
GROUP BY integration_type
ORDER BY count DESC;

-- Verify by platform
SELECT
    platform,
    integration_type,
    COUNT(*) AS count,
    COUNT(*) FILTER (WHERE is_active = TRUE) AS active_count
FROM platform_integrations
GROUP BY platform, integration_type
ORDER BY platform, integration_type;

-- Check for NULL values in critical fields
SELECT
    COUNT(*) FILTER (WHERE platform IS NULL) AS null_platform,
    COUNT(*) FILTER (WHERE integration_type IS NULL) AS null_integration_type,
    COUNT(*) FILTER (WHERE access_token IS NULL) AS null_access_token,
    COUNT(*) FILTER (WHERE is_active = TRUE AND access_token IS NULL) AS active_missing_token
FROM platform_integrations;

-- Compare original vs migrated (source table counts)
SELECT
    'platform_configs (source)' AS source,
    COUNT(*) AS count
FROM platform_configs
UNION ALL
SELECT
    'music_oauth_tokens (source)',
    COUNT(*)
FROM music_oauth_tokens
UNION ALL
SELECT
    'platform_integrations (target)',
    COUNT(*)
FROM platform_integrations;

-- List sample credentials by platform (non-sensitive fields only)
SELECT
    platform,
    integration_type,
    community_id,
    user_id,
    client_id,
    is_active,
    is_encrypted,
    created_at,
    updated_at
FROM platform_integrations
ORDER BY platform, integration_type
LIMIT 20;

*/
  $body$;
END
$outer$;
