-- ============================================================================
-- LOCAL DEVELOPMENT ONLY -- docker-compose's postgres initdb hook.
--
-- SECURITY (H-1, CWE-798): the `*_dev_changeme` / `dev123` passwords below are
-- repo-public development credentials. This file is mounted ONLY by
-- docker-compose.yml (infra-postgres -> /docker-entrypoint-initdb.d); the Helm
-- chart ships its own credential-free init script (templates/infrastructure/
-- postgres.yaml) and NO container image, chart, or migration copies this file
-- (asserted by alembic/tests/test_repo_credentials_guard.py). On any shared
-- database these roles are stripped of LOGIN + password by
-- alembic/versions/0049_per_service_db_roles.py. Never run this file against
-- alpha/beta/gamma/production, and never reuse any value from it there.
-- ============================================================================

-- Waddles Development Database Initialization
-- This script sets up the basic database structure for development

-- Grant replication permissions to waddlebot user for read replicas
ALTER USER waddlebot WITH REPLICATION;

-- Create Kong database and user
-- Kong requires its own database separate from Waddles
CREATE DATABASE kong;

-- Create Kong user with password (must match KONG_PG_PASSWORD in docker-compose.yml)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'kong') THEN
        CREATE ROLE kong WITH LOGIN PASSWORD 'kong_db_pass_change_me';
    END IF;
END
$$;

-- Grant all privileges on kong database to kong user
GRANT ALL PRIVILEGES ON DATABASE kong TO kong;

-- Connect to kong database to grant schema permissions
\c kong

-- Grant schema permissions to kong user
GRANT ALL ON SCHEMA public TO kong;
ALTER SCHEMA public OWNER TO kong;

-- Switch to Waddles database for remaining setup
\c waddlebot

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Create schemas for different modules
CREATE SCHEMA IF NOT EXISTS public;
CREATE SCHEMA IF NOT EXISTS portal;
CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS router;

-- Set default search path
ALTER DATABASE waddlebot SET search_path TO public, portal, identity, router;

-- Create a development user with appropriate permissions
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'waddlebot_dev') THEN
        CREATE ROLE waddlebot_dev WITH LOGIN PASSWORD 'dev123';
    END IF;
END
$$;

-- Create module users for all 34 modules
-- ACTION MODULES - PUSHING (7)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'discord_action') THEN
        CREATE ROLE discord_action WITH LOGIN PASSWORD 'mod_discord_action_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'gcp_functions_action') THEN
        CREATE ROLE gcp_functions_action WITH LOGIN PASSWORD 'mod_gcp_functions_action_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'lambda_action') THEN
        CREATE ROLE lambda_action WITH LOGIN PASSWORD 'mod_lambda_action_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'openwhisk_action') THEN
        CREATE ROLE openwhisk_action WITH LOGIN PASSWORD 'mod_openwhisk_action_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'slack_action') THEN
        CREATE ROLE slack_action WITH LOGIN PASSWORD 'mod_slack_action_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'twitch_action') THEN
        CREATE ROLE twitch_action WITH LOGIN PASSWORD 'mod_twitch_action_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'youtube_action') THEN
        CREATE ROLE youtube_action WITH LOGIN PASSWORD 'mod_youtube_action_dev_changeme';
    END IF;
END
$$;

-- ACTION MODULES - INTERACTIVE (10)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ai_interaction') THEN
        CREATE ROLE ai_interaction WITH LOGIN PASSWORD 'mod_ai_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'alias_interaction') THEN
        CREATE ROLE alias_interaction WITH LOGIN PASSWORD 'mod_alias_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'calendar_interaction') THEN
        CREATE ROLE calendar_interaction WITH LOGIN PASSWORD 'mod_calendar_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'inventory_interaction') THEN
        CREATE ROLE inventory_interaction WITH LOGIN PASSWORD 'mod_inventory_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'loyalty_interaction') THEN
        CREATE ROLE loyalty_interaction WITH LOGIN PASSWORD 'mod_loyalty_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'memories_interaction') THEN
        CREATE ROLE memories_interaction WITH LOGIN PASSWORD 'mod_memories_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'quote_interaction') THEN
        CREATE ROLE quote_interaction WITH LOGIN PASSWORD 'mod_quote_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'shoutout_interaction') THEN
        CREATE ROLE shoutout_interaction WITH LOGIN PASSWORD 'mod_shoutout_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'spotify_interaction') THEN
        CREATE ROLE spotify_interaction WITH LOGIN PASSWORD 'mod_spotify_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'youtube_music_interaction') THEN
        CREATE ROLE youtube_music_interaction WITH LOGIN PASSWORD 'mod_youtube_music_interaction_dev_changeme';
    END IF;
END
$$;

-- TRIGGER MODULES (5)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'discord_trigger') THEN
        CREATE ROLE discord_trigger WITH LOGIN PASSWORD 'mod_discord_trigger_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'kick_trigger') THEN
        CREATE ROLE kick_trigger WITH LOGIN PASSWORD 'mod_kick_trigger_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'slack_trigger') THEN
        CREATE ROLE slack_trigger WITH LOGIN PASSWORD 'mod_slack_trigger_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'twitch_trigger') THEN
        CREATE ROLE twitch_trigger WITH LOGIN PASSWORD 'mod_twitch_trigger_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'youtube_live_trigger') THEN
        CREATE ROLE youtube_live_trigger WITH LOGIN PASSWORD 'mod_youtube_live_trigger_dev_changeme';
    END IF;
END
$$;

-- CORE MODULES (12)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ai_researcher') THEN
        CREATE ROLE ai_researcher WITH LOGIN PASSWORD 'mod_ai_researcher_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'analytics') THEN
        CREATE ROLE analytics WITH LOGIN PASSWORD 'mod_analytics_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'browser_source') THEN
        CREATE ROLE browser_source WITH LOGIN PASSWORD 'mod_browser_source_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'community') THEN
        CREATE ROLE community WITH LOGIN PASSWORD 'mod_community_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'credential_manager') THEN
        CREATE ROLE credential_manager WITH LOGIN PASSWORD 'mod_credential_manager_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'engagement') THEN
        CREATE ROLE engagement WITH LOGIN PASSWORD 'mod_engagement_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'mod_router') THEN
        CREATE ROLE mod_router WITH LOGIN PASSWORD 'mod_router_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'hub_admin') THEN
        CREATE ROLE hub_admin WITH LOGIN PASSWORD 'hub_admin_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'clip_interaction') THEN
        CREATE ROLE clip_interaction WITH LOGIN PASSWORD 'mod_clip_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'lfg_interaction') THEN
        CREATE ROLE lfg_interaction WITH LOGIN PASSWORD 'mod_lfg_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'server_status_interaction') THEN
        CREATE ROLE server_status_interaction WITH LOGIN PASSWORD 'mod_server_status_interaction_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'identity') THEN
        CREATE ROLE identity WITH LOGIN PASSWORD 'mod_identity_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'labels') THEN
        CREATE ROLE labels WITH LOGIN PASSWORD 'mod_labels_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'reputation') THEN
        CREATE ROLE reputation WITH LOGIN PASSWORD 'mod_reputation_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'security') THEN
        CREATE ROLE security WITH LOGIN PASSWORD 'mod_security_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'video_proxy') THEN
        CREATE ROLE video_proxy WITH LOGIN PASSWORD 'mod_video_proxy_dev_changeme';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'workflow') THEN
        CREATE ROLE workflow WITH LOGIN PASSWORD 'mod_workflow_dev_changeme';
    END IF;
END
$$;

-- Grant permissions
GRANT CONNECT ON DATABASE waddlebot TO waddlebot_dev;
GRANT USAGE ON SCHEMA public, portal, identity, router TO waddlebot_dev;
GRANT CREATE ON SCHEMA public, portal, identity, router TO waddlebot_dev;

-- Grant permissions for all module users
-- ACTION MODULES - PUSHING
GRANT CONNECT ON DATABASE waddlebot TO discord_action;
GRANT USAGE ON SCHEMA public TO discord_action;

GRANT CONNECT ON DATABASE waddlebot TO gcp_functions_action;
GRANT USAGE ON SCHEMA public TO gcp_functions_action;

GRANT CONNECT ON DATABASE waddlebot TO lambda_action;
GRANT USAGE ON SCHEMA public TO lambda_action;

GRANT CONNECT ON DATABASE waddlebot TO openwhisk_action;
GRANT USAGE ON SCHEMA public TO openwhisk_action;

GRANT CONNECT ON DATABASE waddlebot TO slack_action;
GRANT USAGE ON SCHEMA public TO slack_action;

GRANT CONNECT ON DATABASE waddlebot TO twitch_action;
GRANT USAGE ON SCHEMA public TO twitch_action;

GRANT CONNECT ON DATABASE waddlebot TO youtube_action;
GRANT USAGE ON SCHEMA public TO youtube_action;

-- ACTION MODULES - INTERACTIVE
GRANT CONNECT ON DATABASE waddlebot TO ai_interaction;
GRANT USAGE ON SCHEMA public TO ai_interaction;

GRANT CONNECT ON DATABASE waddlebot TO alias_interaction;
GRANT USAGE ON SCHEMA public TO alias_interaction;

GRANT CONNECT ON DATABASE waddlebot TO calendar_interaction;
GRANT USAGE ON SCHEMA public TO calendar_interaction;

GRANT CONNECT ON DATABASE waddlebot TO inventory_interaction;
GRANT USAGE ON SCHEMA public TO inventory_interaction;

GRANT CONNECT ON DATABASE waddlebot TO loyalty_interaction;
GRANT USAGE ON SCHEMA public TO loyalty_interaction;

GRANT CONNECT ON DATABASE waddlebot TO memories_interaction;
GRANT USAGE ON SCHEMA public TO memories_interaction;

GRANT CONNECT ON DATABASE waddlebot TO quote_interaction;
GRANT USAGE ON SCHEMA public TO quote_interaction;

GRANT CONNECT ON DATABASE waddlebot TO shoutout_interaction;
GRANT USAGE ON SCHEMA public TO shoutout_interaction;

GRANT CONNECT ON DATABASE waddlebot TO spotify_interaction;
GRANT USAGE ON SCHEMA public TO spotify_interaction;

GRANT CONNECT ON DATABASE waddlebot TO youtube_music_interaction;
GRANT USAGE ON SCHEMA public TO youtube_music_interaction;

-- TRIGGER MODULES
GRANT CONNECT ON DATABASE waddlebot TO discord_trigger;
GRANT USAGE ON SCHEMA public TO discord_trigger;

GRANT CONNECT ON DATABASE waddlebot TO kick_trigger;
GRANT USAGE ON SCHEMA public TO kick_trigger;

GRANT CONNECT ON DATABASE waddlebot TO slack_trigger;
GRANT USAGE ON SCHEMA public TO slack_trigger;

GRANT CONNECT ON DATABASE waddlebot TO twitch_trigger;
GRANT USAGE ON SCHEMA public TO twitch_trigger;

GRANT CONNECT ON DATABASE waddlebot TO youtube_live_trigger;
GRANT USAGE ON SCHEMA public TO youtube_live_trigger;

-- CORE MODULES
GRANT CONNECT ON DATABASE waddlebot TO ai_researcher;
GRANT USAGE ON SCHEMA public TO ai_researcher;

GRANT CONNECT ON DATABASE waddlebot TO analytics;
GRANT USAGE ON SCHEMA public TO analytics;

GRANT CONNECT ON DATABASE waddlebot TO browser_source;
GRANT USAGE ON SCHEMA public TO browser_source;

GRANT CONNECT ON DATABASE waddlebot TO community;
GRANT USAGE ON SCHEMA public TO community;

GRANT CONNECT ON DATABASE waddlebot TO credential_manager;
GRANT USAGE ON SCHEMA public TO credential_manager;

GRANT CONNECT ON DATABASE waddlebot TO engagement;
GRANT USAGE ON SCHEMA public TO engagement;

GRANT CONNECT ON DATABASE waddlebot TO identity;
GRANT USAGE ON SCHEMA public TO identity;

GRANT CONNECT ON DATABASE waddlebot TO labels;
GRANT USAGE ON SCHEMA public TO labels;

GRANT CONNECT ON DATABASE waddlebot TO reputation;
GRANT USAGE ON SCHEMA public TO reputation;

GRANT CONNECT ON DATABASE waddlebot TO security;
GRANT USAGE ON SCHEMA public TO security;

GRANT CONNECT ON DATABASE waddlebot TO video_proxy;
GRANT USAGE ON SCHEMA public TO video_proxy;

GRANT CONNECT ON DATABASE waddlebot TO workflow;
GRANT USAGE ON SCHEMA public TO workflow;

GRANT CONNECT ON DATABASE waddlebot TO mod_router;
GRANT USAGE ON SCHEMA public TO mod_router;

GRANT CONNECT ON DATABASE waddlebot TO hub_admin;
GRANT USAGE ON SCHEMA public TO hub_admin;
GRANT CREATE ON SCHEMA public TO hub_admin;

GRANT CONNECT ON DATABASE waddlebot TO clip_interaction;
GRANT USAGE ON SCHEMA public TO clip_interaction;

GRANT CONNECT ON DATABASE waddlebot TO lfg_interaction;
GRANT USAGE ON SCHEMA public TO lfg_interaction;

GRANT CONNECT ON DATABASE waddlebot TO server_status_interaction;
GRANT USAGE ON SCHEMA public TO server_status_interaction;

-- Create indexes for common query patterns
-- These will be created by py4web/pydal as needed, but we can prepare some common ones

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Insert some development data (optional)
-- This would be handled by the application initialization

-- Platform configuration table (for storing OAuth credentials)
CREATE TABLE IF NOT EXISTS platform_configs (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(50) NOT NULL,
    config_key VARCHAR(100) NOT NULL,
    config_value TEXT,
    is_encrypted BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER,
    UNIQUE(platform, config_key)
);

CREATE INDEX IF NOT EXISTS idx_platform_configs_platform ON platform_configs(platform);

-- Unified Users table (local login centric)
CREATE TABLE IF NOT EXISTS hub_users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE,
    username VARCHAR(100),
    password_hash VARCHAR(255),
    avatar_url TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    is_super_admin BOOLEAN DEFAULT FALSE,
    email_verified BOOLEAN DEFAULT FALSE,
    email_verification_token VARCHAR(100),
    email_verification_expires TIMESTAMP,
    last_login TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Global hub settings
CREATE TABLE IF NOT EXISTS hub_settings (
    id SERIAL PRIMARY KEY,
    setting_key VARCHAR(100) UNIQUE NOT NULL,
    setting_value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES hub_users(id)
);

ALTER TABLE hub_users OWNER TO hub_admin;
ALTER TABLE hub_settings OWNER TO hub_admin;

CREATE INDEX IF NOT EXISTS idx_hub_users_email ON hub_users(email);
CREATE INDEX IF NOT EXISTS idx_hub_users_username ON hub_users(username);

-- Initialize default hub settings for signup configuration
INSERT INTO hub_settings (setting_key, setting_value, updated_at) VALUES
    ('signup_enabled', 'true', NOW()),
    ('email_configured', 'false', NOW()),
    ('signup_allowed_domains', '', NOW())
ON CONFLICT (setting_key) DO NOTHING;


-- AI insights table
CREATE TABLE IF NOT EXISTS ai_insights (
    id SERIAL PRIMARY KEY,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    insight_type VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    summary TEXT,
    metadata JSONB DEFAULT '{}',
    data JSONB DEFAULT '{}',
    confidence_score DECIMAL(3,2) CHECK (confidence_score >= 0 AND confidence_score <= 1),
    priority VARCHAR(20) DEFAULT 'normal',
    status VARCHAR(50) DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_ai_insights_community ON ai_insights(community_id);
CREATE INDEX IF NOT EXISTS idx_ai_insights_type ON ai_insights(insight_type);
CREATE INDEX IF NOT EXISTS idx_ai_insights_status ON ai_insights(status);
CREATE INDEX IF NOT EXISTS idx_ai_insights_created ON ai_insights(created_at DESC);

