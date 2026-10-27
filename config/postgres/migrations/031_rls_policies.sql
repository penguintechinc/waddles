-- Migration 031: Comprehensive Row-Level Security (RLS) Policies
-- Purpose: Enforce per-module credential isolation and column-level access control
-- Scope: 34+ modules (5 triggers, 7 actions, 10+ interactive, 6 core, router, credential manager)
-- Created: 2026-02-05

BEGIN;

-- ============================================================================
-- PART 1: ENABLE RLS ON PLATFORM_INTEGRATIONS TABLE
-- ============================================================================

ALTER TABLE platform_integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_integrations FORCE ROW LEVEL SECURITY;

-- ============================================================================
-- PART 1A: HUB ADMIN POLICY (Full Access)
-- ============================================================================

CREATE POLICY hub_admin_all ON platform_integrations
    FOR ALL
    TO hub_admin
    USING (TRUE)
    WITH CHECK (TRUE);

-- ============================================================================
-- PART 1B: CREDENTIAL MANAGER POLICY (Full Access to platform_integrations)
-- ============================================================================

CREATE POLICY credential_manager_all ON platform_integrations
    FOR ALL
    TO mod_credential_manager
    USING (TRUE)
    WITH CHECK (TRUE);

-- ============================================================================
-- PART 1C: TWITCH MODULE POLICIES (Action + Trigger)
-- ============================================================================

-- Twitch Trigger: Read-only SELECT on twitch platform
CREATE POLICY twitch_trigger_select ON platform_integrations
    FOR SELECT
    TO mod_trigger_twitch
    USING (platform = 'twitch' AND is_active = TRUE);

-- Twitch Action: SELECT on twitch platform
CREATE POLICY twitch_action_select ON platform_integrations
    FOR SELECT
    TO mod_action_twitch
    USING (platform = 'twitch' AND is_active = TRUE);

-- Twitch Action: UPDATE on twitch platform
CREATE POLICY twitch_action_update ON platform_integrations
    FOR UPDATE
    TO mod_action_twitch
    USING (platform = 'twitch')
    WITH CHECK (platform = 'twitch');

-- ============================================================================
-- PART 1D: SLACK MODULE POLICIES (Action + Trigger)
-- ============================================================================

-- Slack Trigger: Read-only SELECT on slack platform
CREATE POLICY slack_trigger_select ON platform_integrations
    FOR SELECT
    TO mod_trigger_slack
    USING (platform = 'slack' AND is_active = TRUE);

-- Slack Action: SELECT on slack platform
CREATE POLICY slack_action_select ON platform_integrations
    FOR SELECT
    TO mod_action_slack
    USING (platform = 'slack' AND is_active = TRUE);

-- Slack Action: UPDATE on slack platform
CREATE POLICY slack_action_update ON platform_integrations
    FOR UPDATE
    TO mod_action_slack
    USING (platform = 'slack')
    WITH CHECK (platform = 'slack');

-- Slack Action: INSERT on slack platform
CREATE POLICY slack_action_insert ON platform_integrations
    FOR INSERT
    TO mod_action_slack
    WITH CHECK (platform = 'slack');

-- ============================================================================
-- PART 1E: DISCORD MODULE POLICIES (Action + Trigger)
-- ============================================================================

-- Discord Trigger: Read-only SELECT on discord platform
CREATE POLICY discord_trigger_select ON platform_integrations
    FOR SELECT
    TO mod_trigger_discord
    USING (platform = 'discord' AND is_active = TRUE);

-- Discord Action: SELECT on discord platform
CREATE POLICY discord_action_select ON platform_integrations
    FOR SELECT
    TO mod_action_discord
    USING (platform = 'discord' AND is_active = TRUE);

-- Discord Action: UPDATE on discord platform
CREATE POLICY discord_action_update ON platform_integrations
    FOR UPDATE
    TO mod_action_discord
    USING (platform = 'discord')
    WITH CHECK (platform = 'discord');

-- Discord Action: INSERT on discord platform
CREATE POLICY discord_action_insert ON platform_integrations
    FOR INSERT
    TO mod_action_discord
    WITH CHECK (platform = 'discord');

-- ============================================================================
-- PART 1F: YOUTUBE MODULE POLICIES (Action + Trigger)
-- ============================================================================

-- YouTube Trigger: Read-only SELECT on youtube platform
CREATE POLICY youtube_trigger_select ON platform_integrations
    FOR SELECT
    TO mod_trigger_youtube
    USING (platform = 'youtube' AND is_active = TRUE);

-- YouTube Action: SELECT on youtube platform
CREATE POLICY youtube_action_select ON platform_integrations
    FOR SELECT
    TO mod_action_youtube
    USING (platform = 'youtube' AND is_active = TRUE);

-- YouTube Action: UPDATE on youtube platform
CREATE POLICY youtube_action_update ON platform_integrations
    FOR UPDATE
    TO mod_action_youtube
    USING (platform = 'youtube')
    WITH CHECK (platform = 'youtube');

-- YouTube Action: INSERT on youtube platform
CREATE POLICY youtube_action_insert ON platform_integrations
    FOR INSERT
    TO mod_action_youtube
    WITH CHECK (platform = 'youtube');

-- ============================================================================
-- PART 1G: KICK MODULE POLICIES (Trigger only, no action module yet)
-- ============================================================================

-- Kick Trigger: Read-only SELECT on kick platform
CREATE POLICY kick_trigger_select ON platform_integrations
    FOR SELECT
    TO mod_trigger_kick
    USING (platform = 'kick' AND is_active = TRUE);

-- ============================================================================
-- PART 1H: SPOTIFY INTERACTIVE MODULE (User OAuth, read/write)
-- ============================================================================

-- Spotify Interaction: SELECT on spotify platform
CREATE POLICY spotify_interaction_select ON platform_integrations
    FOR SELECT
    TO mod_interactive_spotify
    USING (platform = 'spotify' AND is_active = TRUE);

-- Spotify Interaction: UPDATE on spotify platform
CREATE POLICY spotify_interaction_update ON platform_integrations
    FOR UPDATE
    TO mod_interactive_spotify
    USING (platform = 'spotify')
    WITH CHECK (platform = 'spotify');

-- Spotify Interaction: INSERT on spotify platform
CREATE POLICY spotify_interaction_insert ON platform_integrations
    FOR INSERT
    TO mod_interactive_spotify
    WITH CHECK (platform = 'spotify');

-- Spotify Interaction: DELETE on spotify platform
CREATE POLICY spotify_interaction_delete ON platform_integrations
    FOR DELETE
    TO mod_interactive_spotify
    USING (platform = 'spotify');

-- ============================================================================
-- PART 1I: YOUTUBE MUSIC INTERACTIVE MODULE (Community OAuth, read-only)
-- ============================================================================

-- YouTube Music Interaction: SELECT on youtube platform with community_oauth integration_type
CREATE POLICY ytmusic_interaction_select ON platform_integrations
    FOR SELECT
    TO mod_interactive_ytmusic
    USING (platform = 'youtube' AND integration_type = 'community_oauth' AND is_active = TRUE);

-- YouTube Music Interaction: UPDATE community oauth
CREATE POLICY ytmusic_interaction_update ON platform_integrations
    FOR UPDATE
    TO mod_interactive_ytmusic
    USING (platform = 'youtube' AND integration_type = 'community_oauth')
    WITH CHECK (platform = 'youtube' AND integration_type = 'community_oauth');

-- ============================================================================
-- PART 1J: OTHER PUSHING ACTION MODULES (Read-only on their platforms if applicable)
-- ============================================================================

-- Lambda Action: SELECT (if Lambda-specific platform exists)
-- GCP Functions Action: SELECT (if GCP-specific platform exists)
-- OpenWhisk Action: SELECT (if OpenWhisk-specific platform exists)

-- Note: These modules may not use platform_integrations directly;
-- they are included for completeness and future extensibility

-- ============================================================================
-- PART 1K: OTHER INTERACTIVE ACTION MODULES (No platform_integrations access by default)
-- ============================================================================

-- AI Interaction: No direct platform_integrations access (handled by other modules)
-- Alias Interaction: No platform_integrations access
-- Shoutout Interaction: No platform_integrations access
-- Inventory Interaction: No platform_integrations access
-- Calendar Interaction: No platform_integrations access
-- Memories Interaction: No platform_integrations access
-- Loyalty Interaction: No platform_integrations access
-- Quote Interaction: No platform_integrations access

-- ============================================================================
-- PART 1L: CORE MODULES WITH PLATFORM_INTEGRATIONS NEEDS
-- ============================================================================

-- Router Module: SELECT on all active platform integrations for routing decisions
CREATE POLICY router_select_all ON platform_integrations
    FOR SELECT
    TO mod_router
    USING (is_active = TRUE);

-- Identity Core: No direct platform_integrations access (user management only)
-- Labels Core: No platform_integrations access
-- Browser Source Core: No platform_integrations access
-- Security Core: SELECT on all for audit/compliance
CREATE POLICY security_core_select_all ON platform_integrations
    FOR SELECT
    TO mod_core_security
    USING (TRUE);

-- Workflow Core: SELECT on all for workflow execution
CREATE POLICY workflow_core_select_all ON platform_integrations
    FOR SELECT
    TO mod_core_workflow
    USING (is_active = TRUE);

-- Community Module: No direct platform_integrations access
-- Reputation Module: No platform_integrations access
-- Analytics Core: SELECT on all for analytics
CREATE POLICY analytics_select_all ON platform_integrations
    FOR SELECT
    TO mod_core_analytics
    USING (TRUE);

-- AI Researcher: No platform_integrations access
-- Video Proxy: No platform_integrations access
-- Engagement Module: No platform_integrations access
-- Module RTC: No platform_integrations access
-- Unified Music Module: SELECT on music-related platforms (if applicable)

-- ============================================================================
-- PART 2: COLUMN-LEVEL GRANTS ON hub_users TABLE
-- ============================================================================

-- Revoke default public access to hub_users
REVOKE ALL ON hub_users FROM PUBLIC;

-- ============================================================================
-- PART 2A: HUB ADMIN - Full Access
-- ============================================================================

GRANT ALL ON hub_users TO hub_admin;

-- ============================================================================
-- PART 2B: ACTION MODULES - Limited Read Access
-- Safe columns: id, email, username, avatar_url, is_active
-- ============================================================================

-- Twitch Action
GRANT SELECT (id, email, username, avatar_url, is_active) ON hub_users TO mod_action_twitch;

-- Discord Action
GRANT SELECT (id, email, username, avatar_url, is_active) ON hub_users TO mod_action_discord;

-- Slack Action
GRANT SELECT (id, email, username, avatar_url, is_active) ON hub_users TO mod_action_slack;

-- YouTube Action
GRANT SELECT (id, email, username, avatar_url, is_active) ON hub_users TO mod_action_youtube;

-- Lambda Action (if needed)
-- GRANT SELECT (id, username, is_active) ON hub_users TO mod_action_lambda;

-- GCP Functions Action (if needed)
-- GRANT SELECT (id, username, is_active) ON hub_users TO mod_action_gcp;

-- ============================================================================
-- PART 2C: TRIGGER MODULES - Minimal Read Access
-- Safe columns: id only (to identify users)
-- ============================================================================

-- Twitch Trigger
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_twitch;

-- Discord Trigger
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_discord;

-- Slack Trigger
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_slack;

-- YouTube Trigger
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_youtube;

-- Kick Trigger
GRANT SELECT (id, username, is_active) ON hub_users TO mod_trigger_kick;

-- ============================================================================
-- PART 2D: INTERACTIVE ACTION MODULES - Limited Read Access
-- ============================================================================

-- AI Interaction
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_ai;

-- Alias Interaction
-- (Does not need hub_users access)

-- Shoutout Interaction
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_shoutout;

-- Inventory Interaction
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_inventory;

-- Calendar Interaction
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_calendar;

-- Memories Interaction
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_memories;

-- Loyalty Interaction
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_loyalty;

-- Quote Interaction
GRANT SELECT (id, username, is_active) ON hub_users TO mod_interactive_quote;

-- Spotify Interaction
-- (May need special handling for OAuth user associations)

-- YouTube Music Interaction
-- (May need special handling for OAuth user associations)

-- ============================================================================
-- PART 2E: CORE MODULES - Appropriate Access Levels
-- ============================================================================

-- Identity Core: Full access for user management
GRANT SELECT, INSERT, UPDATE ON hub_users TO mod_core_identity;

-- Labels Core: Minimal read
-- (Does not need hub_users access typically)

-- Browser Source Core: Minimal read
-- (Does not need hub_users access typically)

-- Security Core: Full read for security audits
GRANT SELECT ON hub_users TO mod_core_security;

-- Workflow Core: Read for workflow user context
GRANT SELECT (id, username, is_active) ON hub_users TO mod_core_workflow;

-- Community Module: Read for member management
GRANT SELECT (id, username, email, is_active) ON hub_users TO mod_core_community;

-- Reputation Module: Read for reputation tracking
GRANT SELECT (id, username, is_active) ON hub_users TO mod_core_reputation;

-- Analytics Core: Full read for analytics
GRANT SELECT ON hub_users TO mod_core_analytics;

-- AI Researcher: Read for AI insights
-- GRANT SELECT ON hub_users TO mod_core_ai_researcher;

-- Video Proxy: Minimal read
-- (Does not need hub_users access typically)

-- Engagement Module: Read for engagement tracking
GRANT SELECT (id, username, is_active) ON hub_users TO mod_core_engagement;

-- Module RTC: Read for RTC user context
GRANT SELECT (id, username, is_active) ON hub_users TO mod_core_rtc;

-- Unified Music Module: Minimal read
-- (Handled by specific interactive modules)

-- ============================================================================
-- PART 2F: ROUTER & CREDENTIAL MANAGER
-- ============================================================================

-- Router Module: Broad read for routing context
GRANT SELECT ON hub_users TO mod_router;

-- Credential Manager: Limited read (user context only)
GRANT SELECT (id, username, is_active) ON hub_users TO mod_credential_manager;

-- ============================================================================
-- PART 3: PER-MODULE TABLE GRANTS
-- ============================================================================

-- ============================================================================
-- PART 3A: CREDENTIAL MANAGER - Full access to credential tables
-- ============================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON platform_integrations TO mod_credential_manager;
GRANT SELECT, INSERT, UPDATE, DELETE ON credential_access_log TO mod_credential_manager;

-- ============================================================================
-- PART 3B: IDENTITY CORE - Full access to identity tables
-- ============================================================================

GRANT SELECT, INSERT, UPDATE ON hub_users TO mod_core_identity;
GRANT SELECT, INSERT, UPDATE ON hub_user_identities TO mod_core_identity;

-- ============================================================================
-- PART 3C: REPUTATION MODULE - Full access to reputation tables (if exists)
-- ============================================================================

-- GRANT SELECT, INSERT, UPDATE, DELETE ON reputation_tables TO mod_core_reputation;

-- ============================================================================
-- PART 3D: COMMUNITY MODULE - Full access to community-related tables
-- ============================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON communities TO mod_core_community;
GRANT SELECT, INSERT, UPDATE, DELETE ON community_servers TO mod_core_community;
GRANT SELECT, INSERT, UPDATE, DELETE ON community_members TO mod_core_community;

-- ============================================================================
-- PART 3E: ANALYTICS CORE - SELECT on all tables for analytics
-- ============================================================================

GRANT SELECT ON ALL TABLES IN SCHEMA public TO mod_core_analytics;

-- ============================================================================
-- PART 3F: SECURITY CORE - SELECT on all tables for audit
-- ============================================================================

GRANT SELECT ON ALL TABLES IN SCHEMA public TO mod_core_security;

-- ============================================================================
-- PART 3G: ROUTER MODULE - Broad read access
-- ============================================================================

SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_router');  -- 'servers' has no CREATE TABLE anywhere; helper from 031_scoped_database_users skips absent tables
GRANT SELECT, INSERT, UPDATE ON commands, command_aliases TO mod_router;
-- module_configs has no CREATE TABLE in any migration; skip (with a NOTICE) until it exists.
SELECT grant_privs_if_exists('SELECT, INSERT, UPDATE', ARRAY['module_configs'], 'mod_router');

-- ============================================================================
-- PART 3H: WORKFLOW CORE - Read on workflow tables
-- ============================================================================

SELECT grant_privs_if_exists('SELECT', ARRAY['servers', 'community_servers', 'communities', 'modules'], 'mod_core_workflow');

-- ============================================================================
-- PART 4: SEQUENCE PERMISSIONS (CRITICAL)
-- ============================================================================

-- Grant USAGE on all sequences to all modules (required for INSERT operations)
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO hub_admin;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_router;

-- Trigger modules
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_twitch;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_discord;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_slack;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_youtube;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_trigger_kick;

-- Action modules
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_twitch;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_discord;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_slack;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_youtube;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_lambda;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_action_gcp;

-- Interactive modules
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_ai;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_alias;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_shoutout;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_inventory;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_calendar;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_memories;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_ytmusic;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_spotify;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_loyalty;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_interactive_quote;

-- Core modules
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_labels;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_browser_source;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_identity;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_ai_researcher;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_workflow;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_community;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_reputation;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_analytics;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_security;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_video_proxy;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_engagement;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_core_rtc;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO mod_credential_manager;

-- ============================================================================
-- PART 5: CREDENTIAL ACCESS LOG RLS POLICIES
-- ============================================================================

ALTER TABLE credential_access_log ENABLE ROW LEVEL SECURITY;

-- Hub admin: Full access to all audit logs
CREATE POLICY credential_log_hub_admin_all ON credential_access_log
    FOR ALL
    TO hub_admin
    USING (TRUE)
    WITH CHECK (TRUE);

-- Credential manager: Full access for audit management
CREATE POLICY credential_log_credential_manager_all ON credential_access_log
    FOR ALL
    TO mod_credential_manager
    USING (TRUE)
    WITH CHECK (TRUE);

-- Each module can only see its own audit entries
CREATE POLICY credential_log_module_self_access ON credential_access_log
    FOR SELECT
    TO mod_trigger_twitch, mod_trigger_discord, mod_trigger_slack, mod_trigger_youtube, mod_trigger_kick,
       mod_action_twitch, mod_action_discord, mod_action_slack, mod_action_youtube,
       mod_interactive_spotify, mod_interactive_ytmusic, mod_router
    USING (db_user = current_user);

-- ============================================================================
-- PART 6: FUTURE EXTENSIBILITY & COMMENTS
-- ============================================================================

-- This migration provides comprehensive RLS enforcement for 34+ modules:
--
-- TRIGGER MODULES (5):
--   - mod_trigger_twitch, mod_trigger_discord, mod_trigger_slack
--   - mod_trigger_youtube, mod_trigger_kick
--
-- ACTION MODULES (7):
--   - mod_action_twitch, mod_action_discord, mod_action_slack
--   - mod_action_youtube, mod_action_lambda, mod_action_gcp
--   - mod_action_openwhisk (not yet created)
--
-- INTERACTIVE ACTION MODULES (10):
--   - mod_interactive_ai, mod_interactive_alias, mod_interactive_shoutout
--   - mod_interactive_inventory, mod_interactive_calendar, mod_interactive_memories
--   - mod_interactive_ytmusic, mod_interactive_spotify, mod_interactive_loyalty
--   - mod_interactive_quote
--
-- CORE MODULES (12):
--   - mod_core_labels, mod_core_browser_source, mod_core_identity
--   - mod_core_ai_researcher, mod_core_workflow, mod_core_community
--   - mod_core_reputation, mod_core_analytics, mod_core_security
--   - mod_core_video_proxy, mod_core_engagement, mod_core_rtc
--
-- SPECIAL MODULES (2):
--   - mod_router (routing hub)
--   - mod_credential_manager (credential lifecycle management)
--
-- ADMIN (1):
--   - hub_admin (full administrative access)

COMMIT;
