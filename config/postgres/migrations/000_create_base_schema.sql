-- Migration 000: Base Schema
-- Description: Canonical DDL for the hub-api base schema, extracted from
--              admin/hub_module/backend/src/index.js initializeDatabase().
--              This migration must run BEFORE all others so that subsequent
--              migrations (e.g. 001_add_performance_indexes.sql) can reference
--              tables that already exist on a fresh database.
--
--              All statements use IF NOT EXISTS / DO $$ blocks to be idempotent.
--              Running this against a database that hub-api has already initialised
--              is safe and produces no changes.
--
-- Dependency order:
--   hub_admins
--   hub_users
--   hub_sessions
--   hub_temp_passwords
--   hub_user_identities       (FK → hub_users)
--   community_type (enum)
--   communities
--   community_members          (FK → communities)
--   hub_chat_messages          (FK → communities via app logic, no hard FK in DDL)
--   hub_modules
--   hub_module_installations   (FK → hub_modules)
--   hub_module_reviews         (FK → hub_modules)
--   announcements              (FK → communities, hub_users)
--   announcement_broadcasts    (FK → announcements)
--   community_overlay_tokens   (FK → communities)
--   overlay_access_log
--   analytics_bot_scores       (FK → communities)
--   analytics_suspected_bots   (FK → communities, hub_users)
--   hub_settings               (FK → hub_users)
--   cookie_policy_versions     (FK → hub_users)
--   cookie_consent             (FK → hub_users)
--   cookie_audit_log           (FK → hub_users)
--   hub_oauth_states           (FK → hub_users)
--   hub_user_profiles          (FK → hub_users)
--   platform_configs           (FK → hub_users)
--   audit_log                  (FK → hub_users)
--   community_servers          (FK → communities, hub_users)
-- =============================================================================

-- =============================================================================
-- Shared updated_at trigger function
-- Many later migrations (009_add_shoutout_config, 018, 024, 028, 034, ...)
-- attach triggers to this function before (or without) defining it themselves.
-- Defining it once here removes that forward reference on a fresh-DB replay;
-- CREATE OR REPLACE keeps it idempotent for the files that redefine it.
-- =============================================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- Immutable tsvector wrapper for GENERATED columns
-- PostgreSQL marks to_tsvector() as STABLE, not IMMUTABLE, so it cannot be
-- used directly in GENERATED ALWAYS AS expressions.  This thin wrapper
-- re-declares the same call as IMMUTABLE which is safe in practice because
-- text-search configurations do not change at runtime.
-- =============================================================================
CREATE OR REPLACE FUNCTION immutable_to_tsvector(config regconfig, doc text)
RETURNS tsvector AS $$
BEGIN
  RETURN to_tsvector(config, doc);
END;
$$ LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE;

CREATE OR REPLACE FUNCTION immutable_array_to_string(arr text[], sep text)
RETURNS text AS $$
BEGIN
  RETURN array_to_string(arr, sep);
END;
$$ LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE;

-- =============================================================================
-- hub_admins
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_admins (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    is_active BOOLEAN DEFAULT true,
    is_super_admin BOOLEAN DEFAULT false,
    last_login TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- hub_users
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_users (
    id SERIAL PRIMARY KEY,
    display_name VARCHAR(255),
    username VARCHAR(255) UNIQUE,
    email VARCHAR(255) UNIQUE,
    password_hash VARCHAR(255),
    avatar_url TEXT,
    is_super_admin BOOLEAN DEFAULT false,
    is_vendor BOOLEAN DEFAULT false,
    email_verified BOOLEAN DEFAULT false,
    email_verification_token VARCHAR(255),
    password_reset_token VARCHAR(255),
    password_reset_expires TIMESTAMP,
    last_login TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN DEFAULT true
);

-- =============================================================================
-- hub_sessions
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_sessions (
    id SERIAL PRIMARY KEY,
    session_token TEXT NOT NULL,
    user_id INTEGER,
    platform VARCHAR(50),
    platform_user_id VARCHAR(255),
    platform_username VARCHAR(255),
    avatar_url TEXT,
    is_active BOOLEAN DEFAULT true,
    expires_at TIMESTAMP,
    revoked_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- hub_temp_passwords
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_temp_passwords (
    id SERIAL PRIMARY KEY,
    user_identifier VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    community_id INTEGER,
    force_oauth_link BOOLEAN DEFAULT false,
    linked_oauth_platform VARCHAR(50),
    linked_oauth_user_id VARCHAR(255),
    is_used BOOLEAN DEFAULT false,
    used_at TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- hub_user_identities
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_user_identities (
    id SERIAL PRIMARY KEY,
    hub_user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(255) NOT NULL,
    platform_username VARCHAR(255),
    avatar_url TEXT,
    is_primary BOOLEAN DEFAULT false,
    linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP,
    UNIQUE(hub_user_id, platform),
    UNIQUE(platform, platform_user_id)
);

-- =============================================================================
-- community_type enum
-- =============================================================================
-- On a fresh DB this type does not exist. The Alembic baseline skips this
-- file entirely when schema_migrations already exists (hub-api ran first).
CREATE TYPE community_type AS ENUM (
    'shared_interest_group',
    'gaming',
    'creator',
    'corporate',
    'other'
);

-- =============================================================================
-- communities
-- NOTE: is_global is NOT in the original initializeDatabase() DDL but is
--       referenced by seed_admin.sql and migration 009_backfill_global_community.sql.
--       It is included here so those scripts work on a fresh database.
-- =============================================================================
CREATE TABLE IF NOT EXISTS communities (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    display_name VARCHAR(255),
    description TEXT,
    logo_url TEXT,
    banner_url TEXT,
    primary_platform VARCHAR(50) NOT NULL DEFAULT 'discord',
    platform VARCHAR(50) NOT NULL DEFAULT 'discord',
    platform_server_id VARCHAR(255),
    owner_id VARCHAR(255),
    owner_name VARCHAR(255),
    community_type community_type NOT NULL DEFAULT 'creator',
    join_mode VARCHAR(50) DEFAULT 'open',
    member_count INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT true,
    is_public BOOLEAN DEFAULT true,
    is_global BOOLEAN DEFAULT false,
    config JSONB DEFAULT '{}',
    created_by VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP,
    deleted_by VARCHAR(255)
);

-- =============================================================================
-- community_members
-- =============================================================================
CREATE TABLE IF NOT EXISTS community_members (
    id SERIAL PRIMARY KEY,
    community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
    user_id VARCHAR(255),
    platform VARCHAR(50),
    platform_user_id VARCHAR(255),
    display_name VARCHAR(255),
    avatar_url TEXT,
    bio TEXT,
    social_links JSONB DEFAULT '{}',
    role VARCHAR(50) DEFAULT 'member',
    reputation INTEGER DEFAULT 600,
    is_active BOOLEAN DEFAULT true,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    left_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(community_id, platform, platform_user_id)
);

-- =============================================================================
-- hub_chat_messages
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_chat_messages (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    channel_name VARCHAR(255),
    sender_hub_user_id INTEGER,
    sender_platform VARCHAR(50),
    sender_username VARCHAR(255),
    sender_avatar_url TEXT,
    message_content TEXT NOT NULL,
    message_type VARCHAR(50) DEFAULT 'text',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_community
    ON hub_chat_messages(community_id, created_at DESC);

-- =============================================================================
-- hub_modules
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_modules (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    display_name VARCHAR(255),
    description TEXT,
    version VARCHAR(50),
    author VARCHAR(255),
    category VARCHAR(100),
    icon_url TEXT,
    is_published BOOLEAN DEFAULT false,
    is_core BOOLEAN DEFAULT false,
    config_schema JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- hub_module_installations
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_module_installations (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    module_id INTEGER REFERENCES hub_modules(id),
    installed_by INTEGER,
    config JSONB DEFAULT '{}',
    is_enabled BOOLEAN DEFAULT true,
    installed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(community_id, module_id)
);

-- =============================================================================
-- hub_module_reviews
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_module_reviews (
    id SERIAL PRIMARY KEY,
    module_id INTEGER REFERENCES hub_modules(id),
    community_id INTEGER,
    user_id INTEGER,
    rating INTEGER CHECK (rating >= 1 AND rating <= 5),
    review_text TEXT,
    admin_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- announcements
-- =============================================================================
CREATE TABLE IF NOT EXISTS announcements (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    announcement_type VARCHAR(50) DEFAULT 'general',
    priority INTEGER DEFAULT 0,
    status VARCHAR(50) DEFAULT 'published',
    is_pinned BOOLEAN DEFAULT false,
    broadcast_to_platforms BOOLEAN DEFAULT false,
    broadcasted_platforms JSONB DEFAULT '[]',
    created_by INTEGER REFERENCES hub_users(id),
    created_by_name VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES hub_users(id),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP,
    archived_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_announcements_community
    ON announcements(community_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_announcements_pinned
    ON announcements(community_id, is_pinned, created_at DESC);

-- =============================================================================
-- announcement_broadcasts
-- =============================================================================
CREATE TABLE IF NOT EXISTS announcement_broadcasts (
    id SERIAL PRIMARY KEY,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    community_server_id INTEGER,
    platform VARCHAR(50) NOT NULL,
    channel_id VARCHAR(255),
    status VARCHAR(50) DEFAULT 'pending',
    platform_message_id VARCHAR(255),
    error_message TEXT,
    broadcasted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_announcement_broadcasts_announcement
    ON announcement_broadcasts(announcement_id);

-- =============================================================================
-- community_overlay_tokens
-- =============================================================================
CREATE TABLE IF NOT EXISTS community_overlay_tokens (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    overlay_key VARCHAR(64) NOT NULL UNIQUE,
    previous_key VARCHAR(64),
    is_active BOOLEAN DEFAULT true,
    theme_config JSONB DEFAULT '{}',
    enabled_sources JSONB DEFAULT '["alerts", "chat", "goals", "ticker"]',
    last_accessed TIMESTAMP,
    access_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    rotated_at TIMESTAMP,
    UNIQUE(community_id)
);

-- =============================================================================
-- overlay_access_log
-- =============================================================================
CREATE TABLE IF NOT EXISTS overlay_access_log (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL,
    overlay_key VARCHAR(64),
    ip_address VARCHAR(45),
    user_agent TEXT,
    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_overlay_access_log_community
    ON overlay_access_log(community_id, accessed_at DESC);

-- =============================================================================
-- analytics_bot_scores
-- =============================================================================
CREATE TABLE IF NOT EXISTS analytics_bot_scores (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    score INTEGER NOT NULL CHECK (score >= 0 AND score <= 100),
    grade VARCHAR(1) NOT NULL CHECK (grade IN ('A', 'B', 'C', 'D', 'F')),
    bad_actor_score INTEGER DEFAULT 0,
    reputation_score INTEGER DEFAULT 0,
    security_score INTEGER DEFAULT 0,
    ai_behavioral_score INTEGER DEFAULT 0,
    suspected_bot_count INTEGER DEFAULT 0,
    high_confidence_bot_count INTEGER DEFAULT 0,
    total_users_analyzed INTEGER DEFAULT 0,
    community_size_category VARCHAR(20) DEFAULT 'small',
    calculation_metadata JSONB DEFAULT '{}',
    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(community_id)
);

CREATE INDEX IF NOT EXISTS idx_analytics_bot_scores_community
    ON analytics_bot_scores(community_id);

CREATE INDEX IF NOT EXISTS idx_analytics_bot_scores_grade
    ON analytics_bot_scores(grade);

-- =============================================================================
-- analytics_suspected_bots
-- =============================================================================
CREATE TABLE IF NOT EXISTS analytics_suspected_bots (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(255) NOT NULL,
    username VARCHAR(255),
    confidence_score INTEGER NOT NULL CHECK (confidence_score >= 0 AND confidence_score <= 100),
    detection_reasons JSONB DEFAULT '[]',
    ai_analysis TEXT,
    behavioral_flags JSONB DEFAULT '[]',
    first_detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMP,
    is_confirmed_bot BOOLEAN DEFAULT false,
    is_false_positive BOOLEAN DEFAULT false,
    reviewed_by INTEGER REFERENCES hub_users(id),
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(community_id, platform, platform_user_id)
);

CREATE INDEX IF NOT EXISTS idx_analytics_suspected_bots_community
    ON analytics_suspected_bots(community_id, confidence_score DESC);

-- =============================================================================
-- hub_settings
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_settings (
    id SERIAL PRIMARY KEY,
    setting_key VARCHAR(255) UNIQUE NOT NULL,
    setting_value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES hub_users(id)
);

CREATE INDEX IF NOT EXISTS idx_hub_settings_key
    ON hub_settings(setting_key);

-- =============================================================================
-- cookie_policy_versions
-- =============================================================================
CREATE TABLE IF NOT EXISTS cookie_policy_versions (
    id SERIAL PRIMARY KEY,
    version VARCHAR(50) UNIQUE NOT NULL,
    content TEXT NOT NULL,
    changes_summary TEXT,
    is_active BOOLEAN DEFAULT false,
    effective_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER REFERENCES hub_users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cookie_policy_active
    ON cookie_policy_versions(is_active) WHERE is_active = true;

-- =============================================================================
-- cookie_consent
-- =============================================================================
CREATE TABLE IF NOT EXISTS cookie_consent (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    consent_id VARCHAR(255) UNIQUE NOT NULL,
    preferences JSONB DEFAULT '{"necessary": true, "functional": false, "analytics": false, "marketing": false}',
    consent_version VARCHAR(50) NOT NULL,
    consent_method VARCHAR(50) DEFAULT 'banner',
    ip_address VARCHAR(45),
    user_agent TEXT,
    consented_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cookie_consent_user
    ON cookie_consent(user_id) WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cookie_consent_expires
    ON cookie_consent(expires_at);

-- =============================================================================
-- credential_access_log
-- Audit trail of platform credential access, owned by the credential manager
-- (docs/architecture/table-ownership.md: migration 000). 031_rls_policies and
-- 032_rls_policies enable RLS on it (db_user = current_user self-access policy), but no
-- earlier migration ever created it, so both files failed on a fresh-DB replay.
-- =============================================================================
CREATE TABLE IF NOT EXISTS credential_access_log (
    id BIGSERIAL PRIMARY KEY,
    db_user VARCHAR(255) NOT NULL DEFAULT current_user,
    integration_id INTEGER,
    platform VARCHAR(50),
    community_id INTEGER,
    action VARCHAR(50) NOT NULL,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    detail TEXT,
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_credential_access_log_db_user
    ON credential_access_log(db_user, accessed_at DESC);

-- =============================================================================
-- cookie_audit_log
-- =============================================================================
CREATE TABLE IF NOT EXISTS cookie_audit_log (
    id SERIAL PRIMARY KEY,
    consent_id VARCHAR(255),
    user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    action VARCHAR(50) NOT NULL,
    category VARCHAR(50),
    previous_value BOOLEAN,
    new_value BOOLEAN,
    consent_version VARCHAR(50),
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cookie_audit_log_user
    ON cookie_audit_log(user_id, created_at DESC);

-- =============================================================================
-- hub_oauth_states
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_oauth_states (
    id SERIAL PRIMARY KEY,
    state VARCHAR(255) UNIQUE NOT NULL,
    mode VARCHAR(50) NOT NULL DEFAULT 'login',
    platform VARCHAR(50) NOT NULL,
    user_id INTEGER REFERENCES hub_users(id) ON DELETE CASCADE,
    redirect_uri TEXT,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_hub_oauth_states_expires
    ON hub_oauth_states(expires_at);

-- =============================================================================
-- hub_user_profiles
-- =============================================================================
CREATE TABLE IF NOT EXISTS hub_user_profiles (
    id SERIAL PRIMARY KEY,
    hub_user_id INTEGER UNIQUE NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
    display_name VARCHAR(255),
    bio TEXT,
    location VARCHAR(255),
    location_city VARCHAR(100),
    location_state VARCHAR(100),
    location_country VARCHAR(2),
    website_url VARCHAR(500),
    custom_avatar_url TEXT,
    banner_url TEXT,
    visibility VARCHAR(50) DEFAULT 'public',
    show_activity BOOLEAN DEFAULT true,
    show_communities BOOLEAN DEFAULT true,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- platform_configs
-- =============================================================================
CREATE TABLE IF NOT EXISTS platform_configs (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(50) NOT NULL,
    config_key VARCHAR(100) NOT NULL,
    config_value TEXT,
    is_encrypted BOOLEAN DEFAULT false,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES hub_users(id),
    UNIQUE(platform, config_key)
);

-- =============================================================================
-- audit_log
-- =============================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50),
    target_id VARCHAR(255),
    details JSONB DEFAULT '{}',
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_log_user
    ON audit_log(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_action
    ON audit_log(action, created_at DESC);

-- =============================================================================
-- community_servers
-- =============================================================================
CREATE TABLE IF NOT EXISTS community_servers (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_server_id VARCHAR(255) NOT NULL,
    platform_server_name VARCHAR(255),
    link_type VARCHAR(50) DEFAULT 'standard',
    status VARCHAR(50) DEFAULT 'pending',
    is_primary BOOLEAN DEFAULT false,
    config JSONB DEFAULT '{}',
    added_by INTEGER REFERENCES hub_users(id),
    approved_by INTEGER REFERENCES hub_users(id),
    verified_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(community_id, platform, platform_server_id)
);

CREATE INDEX IF NOT EXISTS idx_community_servers_community
    ON community_servers(community_id);

-- =============================================================================
-- Migration 000 Complete
-- =============================================================================
-- Tables created (27 total):
--   hub_admins, hub_users, hub_sessions, hub_temp_passwords,
--   hub_user_identities, communities, community_members, hub_chat_messages,
--   hub_modules, hub_module_installations, hub_module_reviews, announcements,
--   announcement_broadcasts, community_overlay_tokens, overlay_access_log,
--   analytics_bot_scores, analytics_suspected_bots, hub_settings,
--   cookie_policy_versions, cookie_consent, cookie_audit_log,
--   hub_oauth_states, hub_user_profiles, platform_configs, audit_log,
--   community_servers
-- Types created (1): community_type
-- =============================================================================
