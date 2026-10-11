-- Migration 031: Create scoped PostgreSQL roles for module isolation
-- Principle of Least Privilege: each module gets its own database role.
--
-- SECURITY (H-1, CWE-798): this file contains NO passwords. Roles are created
-- NOLOGIN with no password -- they carry the designed GRANTs/RLS policies that
-- the per-service LOGIN roles in config/postgres/service-roles.yaml inherit
-- (alembic/versions/0055_per_service_db_roles.py), but cannot authenticate.
-- The ONLY way a role created here gets a password is the explicit local/dev
-- opt-in below; it can never fire in alpha/beta/gamma/production because
-- alembic/env.py refuses to stage it there.
--
--   waddles.dev_db_role_pw_suffix   (session GUC, staged by alembic/env.py from
--                                    the WADDLES_DEV_DB_ROLE_PW_SUFFIX env var,
--                                    set ONLY by docker-compose's db-migrations)
--     unset/empty -> NOLOGIN role, never ALTERed on re-apply (production path)
--     set         -> LOGIN with password <role><suffix> (docker-compose dev only)

-- ============================================================================
-- HELPER: Create user if not exists
-- ============================================================================
CREATE OR REPLACE FUNCTION create_user_if_not_exists(
    p_username TEXT
) RETURNS VOID AS $$
DECLARE
    v_suffix TEXT := NULLIF(current_setting('waddles.dev_db_role_pw_suffix', true), '');
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = p_username) THEN
        IF v_suffix IS NULL THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', p_username);
        ELSE
            EXECUTE format('CREATE ROLE %I WITH LOGIN PASSWORD %L', p_username, p_username || v_suffix);
        END IF;
    ELSIF v_suffix IS NOT NULL THEN
        -- local/dev only: keep an existing docker-compose role usable.
        EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', p_username, p_username || v_suffix);
    END IF;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- HELPER: Grant privileges on a list of tables, skipping any table that
-- doesn't exist yet instead of aborting.
--
-- This whole file is sent to Postgres as ONE multi-statement batch by both
-- the baseline runner (alembic/versions/0001_baseline_from_sql_migrations.py)
-- and the repair migration that re-applies it
-- (0005_repair_scoped_users_hub_admin_seed.py) -- a single failing statement
-- aborts the entire batch (simple-query-protocol semantics), which silently
-- rolled back every role/grant in this file on every fresh-DB bootstrap
-- (confirmed: hub_admin/mod_router/mod_core_identity/mod_core_reputation/...
-- were ALL missing, not just the ones after the failure point). Two
-- independent causes of that, both handled by this guard instead of by
-- auditing every table name by hand:
--   1. Dead references -- `servers` and `credential_access_log` have no
--      CREATE TABLE migration anywhere and no application code queries
--      them either (confirmed via repo-wide grep); these always skip.
--   2. Ordering -- e.g. `modules` is real (queried by admin/hub_module's
--      communityController.js/adminController.js) but its CREATE TABLE is
--      046_add_remaining_admin_tables.sql, which sorts AFTER this file, so
--      it doesn't exist yet on the FIRST (baseline) pass. It skips on that
--      pass and is picked up on 0005's unconditional second pass, which
--      runs after the full 000-081 baseline (including 046) has already
--      applied.
--   3. `slack_actions` (queried by action/pushing/slack_action_module) has
--      the same "real table, no CREATE TABLE migration" gap reputation_events
--      had before 080_add_reputation_tables.sql -- out of scope for this
--      fix (doesn't block hub-api/core-identity/core-reputation/core-router),
--      documented here so it isn't silently lost: it always skips until a
--      future migration adds that table.
-- ============================================================================
CREATE OR REPLACE FUNCTION grant_privs_if_exists(
    p_privs TEXT,
    p_tables TEXT[],
    p_role TEXT
) RETURNS VOID AS $$
DECLARE
    v_table TEXT;
BEGIN
    FOREACH v_table IN ARRAY p_tables
    LOOP
        IF to_regclass('public.' || v_table) IS NOT NULL THEN
            EXECUTE format('GRANT %s ON %I TO %I', p_privs, v_table, p_role);
        ELSE
            RAISE NOTICE 'grant_privs_if_exists: skipping % on %.% (table does not exist yet)',
                p_privs, 'public', v_table;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- HUB ADMIN (full access - manages all platform integrations)
-- ============================================================================
SELECT create_user_if_not_exists('hub_admin');
GRANT CONNECT ON DATABASE waddlebot TO hub_admin;
GRANT USAGE, CREATE ON SCHEMA public TO hub_admin;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO hub_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO hub_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO hub_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO hub_admin;

-- ============================================================================
-- ROUTER MODULE (core routing, needs broad read access)
-- ============================================================================
SELECT create_user_if_not_exists('mod_router');
GRANT CONNECT ON DATABASE waddlebot TO mod_router;
GRANT USAGE ON SCHEMA public TO mod_router;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mod_router;
SELECT grant_privs_if_exists('INSERT, UPDATE', ARRAY['commands', 'command_aliases'], 'mod_router');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_router;

-- ============================================================================
-- TRIGGER MODULES (read-heavy, receive events from platforms)
-- ============================================================================

-- Twitch Trigger
SELECT create_user_if_not_exists('mod_trigger_twitch');
GRANT CONNECT ON DATABASE waddlebot TO mod_trigger_twitch;
GRANT USAGE ON SCHEMA public TO mod_trigger_twitch;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_trigger_twitch');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_twitch;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_twitch;

-- Discord Trigger
SELECT create_user_if_not_exists('mod_trigger_discord');
GRANT CONNECT ON DATABASE waddlebot TO mod_trigger_discord;
GRANT USAGE ON SCHEMA public TO mod_trigger_discord;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_trigger_discord');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_discord;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_discord;

-- Slack Trigger
SELECT create_user_if_not_exists('mod_trigger_slack');
GRANT CONNECT ON DATABASE waddlebot TO mod_trigger_slack;
GRANT USAGE ON SCHEMA public TO mod_trigger_slack;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_trigger_slack');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_slack;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_slack;

-- YouTube Trigger
SELECT create_user_if_not_exists('mod_trigger_youtube');
GRANT CONNECT ON DATABASE waddlebot TO mod_trigger_youtube;
GRANT USAGE ON SCHEMA public TO mod_trigger_youtube;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_trigger_youtube');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_youtube;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_youtube;

-- Kick Trigger
SELECT create_user_if_not_exists('mod_trigger_kick');
GRANT CONNECT ON DATABASE waddlebot TO mod_trigger_kick;
GRANT USAGE ON SCHEMA public TO mod_trigger_kick;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_trigger_kick');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_kick;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_kick;

-- ============================================================================
-- ACTION MODULES (push messages to platforms)
-- ============================================================================

-- Twitch Action
SELECT create_user_if_not_exists('mod_action_twitch');
GRANT CONNECT ON DATABASE waddlebot TO mod_action_twitch;
GRANT USAGE ON SCHEMA public TO mod_action_twitch;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_action_twitch');
GRANT SELECT (id, email, username, avatar_url, is_active) ON hub_users TO mod_action_twitch;
GRANT SELECT ON platform_integrations TO mod_action_twitch;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_twitch;

-- Discord Action
SELECT create_user_if_not_exists('mod_action_discord');
GRANT CONNECT ON DATABASE waddlebot TO mod_action_discord;
GRANT USAGE ON SCHEMA public TO mod_action_discord;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_action_discord');
GRANT SELECT (id, email, username, avatar_url, is_active) ON hub_users TO mod_action_discord;
GRANT SELECT ON platform_integrations TO mod_action_discord;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_discord;

-- Slack Action
SELECT create_user_if_not_exists('mod_action_slack');
GRANT CONNECT ON DATABASE waddlebot TO mod_action_slack;
GRANT USAGE ON SCHEMA public TO mod_action_slack;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_action_slack');
GRANT SELECT (id, email, username, avatar_url, is_active) ON hub_users TO mod_action_slack;
GRANT SELECT ON platform_integrations TO mod_action_slack;
SELECT grant_privs_if_exists('SELECT, INSERT, UPDATE, DELETE', ARRAY['slack_actions'], 'mod_action_slack');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_slack;

-- YouTube Action
SELECT create_user_if_not_exists('mod_action_youtube');
GRANT CONNECT ON DATABASE waddlebot TO mod_action_youtube;
GRANT USAGE ON SCHEMA public TO mod_action_youtube;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_action_youtube');
GRANT SELECT (id, email, username, avatar_url, is_active) ON hub_users TO mod_action_youtube;
GRANT SELECT ON platform_integrations TO mod_action_youtube;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_youtube;

-- Lambda Action
SELECT create_user_if_not_exists('mod_action_lambda');
GRANT CONNECT ON DATABASE waddlebot TO mod_action_lambda;
GRANT USAGE ON SCHEMA public TO mod_action_lambda;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_action_lambda');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_lambda;

-- GCP Functions Action
SELECT create_user_if_not_exists('mod_action_gcp');
GRANT CONNECT ON DATABASE waddlebot TO mod_action_gcp;
GRANT USAGE ON SCHEMA public TO mod_action_gcp;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_action_gcp');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_gcp;

-- ============================================================================
-- INTERACTIVE MODULES
-- ============================================================================

-- AI Interaction
SELECT create_user_if_not_exists('mod_interactive_ai');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_ai;
GRANT USAGE ON SCHEMA public TO mod_interactive_ai;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules', 'commands'], 'mod_interactive_ai');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_ai;
GRANT SELECT, INSERT, UPDATE ON ai_insights TO mod_interactive_ai;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_ai;

-- Alias Interaction
SELECT create_user_if_not_exists('mod_interactive_alias');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_alias;
GRANT USAGE ON SCHEMA public TO mod_interactive_alias;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_interactive_alias');
GRANT SELECT, INSERT, UPDATE, DELETE ON command_aliases TO mod_interactive_alias;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_alias;

-- Shoutout Interaction
SELECT create_user_if_not_exists('mod_interactive_shoutout');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_shoutout;
GRANT USAGE ON SCHEMA public TO mod_interactive_shoutout;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_interactive_shoutout');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_shoutout;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_shoutout;

-- Inventory Interaction
SELECT create_user_if_not_exists('mod_interactive_inventory');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_inventory;
GRANT USAGE ON SCHEMA public TO mod_interactive_inventory;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_interactive_inventory');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_inventory;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_inventory;

-- Calendar Interaction
SELECT create_user_if_not_exists('mod_interactive_calendar');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_calendar;
GRANT USAGE ON SCHEMA public TO mod_interactive_calendar;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_interactive_calendar');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_calendar;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_calendar;

-- Memories Interaction
SELECT create_user_if_not_exists('mod_interactive_memories');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_memories;
GRANT USAGE ON SCHEMA public TO mod_interactive_memories;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_interactive_memories');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_memories;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_memories;

-- YouTube Music Interaction
SELECT create_user_if_not_exists('mod_interactive_ytmusic');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_ytmusic;
GRANT USAGE ON SCHEMA public TO mod_interactive_ytmusic;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_interactive_ytmusic');
GRANT SELECT ON platform_integrations TO mod_interactive_ytmusic;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_ytmusic;

-- Spotify Interaction
SELECT create_user_if_not_exists('mod_interactive_spotify');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_spotify;
GRANT USAGE ON SCHEMA public TO mod_interactive_spotify;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_interactive_spotify');
GRANT SELECT, INSERT, UPDATE, DELETE ON platform_integrations TO mod_interactive_spotify;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_spotify;

-- Loyalty Interaction
SELECT create_user_if_not_exists('mod_interactive_loyalty');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_loyalty;
GRANT USAGE ON SCHEMA public TO mod_interactive_loyalty;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_interactive_loyalty');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_loyalty;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_loyalty;

-- Quote Interaction
SELECT create_user_if_not_exists('mod_interactive_quote');
GRANT CONNECT ON DATABASE waddlebot TO mod_interactive_quote;
GRANT USAGE ON SCHEMA public TO mod_interactive_quote;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_interactive_quote');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_quote;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_quote;

-- ============================================================================
-- CORE MODULES
-- ============================================================================

-- Labels Core
SELECT create_user_if_not_exists('mod_core_labels');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_labels;
GRANT USAGE ON SCHEMA public TO mod_core_labels;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_core_labels');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_labels;

-- Browser Source Core
SELECT create_user_if_not_exists('mod_core_browser_source');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_browser_source;
GRANT USAGE ON SCHEMA public TO mod_core_browser_source;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_core_browser_source');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_browser_source;

-- Identity Core
SELECT create_user_if_not_exists('mod_core_identity');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_identity;
GRANT USAGE ON SCHEMA public TO mod_core_identity;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_core_identity');
GRANT SELECT, INSERT, UPDATE ON hub_users, hub_user_identities TO mod_core_identity;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_identity;

-- AI Researcher
SELECT create_user_if_not_exists('mod_core_ai_researcher');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_ai_researcher;
GRANT USAGE ON SCHEMA public TO mod_core_ai_researcher;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_core_ai_researcher');
GRANT SELECT, INSERT, UPDATE ON ai_insights TO mod_core_ai_researcher;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_ai_researcher;

-- Workflow Core
SELECT create_user_if_not_exists('mod_core_workflow');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_workflow;
GRANT USAGE ON SCHEMA public TO mod_core_workflow;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_core_workflow');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_workflow;

-- Community Module
SELECT create_user_if_not_exists('mod_core_community');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_community;
GRANT USAGE ON SCHEMA public TO mod_core_community;
GRANT SELECT, INSERT, UPDATE, DELETE ON communities, community_servers, community_members TO mod_core_community;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'modules'], 'mod_core_community');
GRANT SELECT (id, username, email, is_active) ON hub_users TO mod_core_community;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_community;

-- Reputation Module
SELECT create_user_if_not_exists('mod_core_reputation');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_reputation;
GRANT USAGE ON SCHEMA public TO mod_core_reputation;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_core_reputation');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_core_reputation;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_reputation;

-- Analytics Core
SELECT create_user_if_not_exists('mod_core_analytics');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_analytics;
GRANT USAGE ON SCHEMA public TO mod_core_analytics;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mod_core_analytics;
GRANT INSERT, UPDATE ON ai_insights TO mod_core_analytics;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_analytics;

-- Security Core
SELECT create_user_if_not_exists('mod_core_security');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_security;
GRANT USAGE ON SCHEMA public TO mod_core_security;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mod_core_security;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_security;

-- Video Proxy
SELECT create_user_if_not_exists('mod_core_video_proxy');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_video_proxy;
GRANT USAGE ON SCHEMA public TO mod_core_video_proxy;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_core_video_proxy');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_video_proxy;

-- Engagement Module
SELECT create_user_if_not_exists('mod_core_engagement');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_engagement;
GRANT USAGE ON SCHEMA public TO mod_core_engagement;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_core_engagement');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_core_engagement;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_engagement;

-- Module RTC
SELECT create_user_if_not_exists('mod_core_rtc');
GRANT CONNECT ON DATABASE waddlebot TO mod_core_rtc;
GRANT USAGE ON SCHEMA public TO mod_core_rtc;
SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities'], 'mod_core_rtc');
GRANT SELECT (id, username, is_active) ON hub_users TO mod_core_rtc;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_rtc;

-- Credential Manager (needs full access to platform_integrations)
SELECT create_user_if_not_exists('mod_credential_manager');
GRANT CONNECT ON DATABASE waddlebot TO mod_credential_manager;
GRANT USAGE ON SCHEMA public TO mod_credential_manager;
GRANT SELECT, INSERT, UPDATE ON platform_integrations TO mod_credential_manager;
SELECT grant_privs_if_exists('SELECT, INSERT', ARRAY['credential_access_log'], 'mod_credential_manager');
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_credential_manager;

-- ============================================================================
-- CLEANUP: Drop the helper functions (not needed at runtime)
-- grant_privs_if_exists is intentionally kept -- 0005's repair migration
-- calls it directly on its second, unconditional pass to pick up grants
-- (e.g. `modules`) that didn't exist yet on the first (baseline) pass.
-- ============================================================================
DROP FUNCTION IF EXISTS create_user_if_not_exists(TEXT, TEXT);
DROP FUNCTION IF EXISTS create_user_if_not_exists(TEXT);
