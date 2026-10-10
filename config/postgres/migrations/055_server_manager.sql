-- Migration 055: Server Manager - RCON, Voice Servers & Access Policies

-- 1. Add columns to server_status_configs
ALTER TABLE server_status_configs
    ADD COLUMN IF NOT EXISTS server_type VARCHAR(30) DEFAULT 'status_only'
        CHECK (server_type IN ('status_only', 'rcon', 'mumble', 'teamspeak')),
    ADD COLUMN IF NOT EXISTS host VARCHAR(255),
    ADD COLUMN IF NOT EXISTS game_port INTEGER,
    ADD COLUMN IF NOT EXISTS rcon_port INTEGER,
    ADD COLUMN IF NOT EXISTS credential_enc BYTEA,
    ADD COLUMN IF NOT EXISTS credential_iv BYTEA,
    ADD COLUMN IF NOT EXISTS game_type VARCHAR(50) DEFAULT 'other'
        CHECK (game_type IN (
            'rust', 'minecraft', 'cs2', 'ark', 'valheim', 'palworld',
            'factorio', 'conan_exiles', '7dtd', 'squad', 'unturned',
            'terraria', 'starbound', 'source', 'mumble', 'teamspeak', 'other'
        )),
    ADD COLUMN IF NOT EXISTS visibility VARCHAR(30) DEFAULT 'admin_only'
        CHECK (visibility IN ('admin_only', 'members', 'registered')),
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(100),
    ADD COLUMN IF NOT EXISTS added_by INTEGER REFERENCES hub_users(id),
    ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- 2. Widen chk_status_api_type constraint to include rcon, mumble_ice, ts_serverquery
-- The column created by 042_interactive_new_modules.sql is `status_api_type` (this file
-- used to reference a non-existent `api_type`, so it could never apply). The widened set
-- keeps the original 042 values (steam, riot, custom_url) so existing rows stay valid.
ALTER TABLE server_status_configs
    DROP CONSTRAINT IF EXISTS chk_status_api_type;

ALTER TABLE server_status_configs
    ADD CONSTRAINT chk_status_api_type CHECK (
        status_api_type IN (
            'steam', 'riot', 'custom_url',
            'http_json', 'battlemetrics', 'steam_query',
            'rcon', 'mumble_ice', 'ts_serverquery'
        )
    );

-- 3. Partial unique index on (community_id, host, rcon_port) for rcon servers
CREATE UNIQUE INDEX IF NOT EXISTS uq_server_status_configs_rcon
    ON server_status_configs (community_id, host, rcon_port)
    WHERE deleted_at IS NULL AND server_type = 'rcon';

-- 4. Create rcon_command_log table
CREATE TABLE IF NOT EXISTS rcon_command_log (
    id                  SERIAL PRIMARY KEY,
    server_config_id    INTEGER NOT NULL REFERENCES server_status_configs(id) ON DELETE CASCADE,
    user_id             INTEGER REFERENCES hub_users(id),
    command             TEXT NOT NULL,
    response_summary    TEXT,
    success             BOOLEAN DEFAULT TRUE,
    executed_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rcon_command_log_server_time
    ON rcon_command_log (server_config_id, executed_at DESC);

-- 5. Create server_ban_sync table (schema only, for future use)
CREATE TABLE IF NOT EXISTS server_ban_sync (
    id                  SERIAL PRIMARY KEY,
    community_id        INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    server_config_id    INTEGER NOT NULL REFERENCES server_status_configs(id) ON DELETE CASCADE,
    sync_enabled        BOOLEAN DEFAULT FALSE,
    sync_direction      VARCHAR(20) DEFAULT 'bidirectional'
        CHECK (sync_direction IN ('to_server', 'from_server', 'bidirectional')),
    last_synced_at      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (community_id, server_config_id)
);

-- 6. Create server_access_policies table
CREATE TABLE IF NOT EXISTS server_access_policies (
    id                          SERIAL PRIMARY KEY,
    server_config_id            INTEGER NOT NULL REFERENCES server_status_configs(id) ON DELETE CASCADE,
    community_id                INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    require_community_member    BOOLEAN DEFAULT FALSE,
    auto_kick_enabled           BOOLEAN DEFAULT FALSE,
    auto_kick_threshold         INTEGER DEFAULT 450,
    auto_ban_enabled            BOOLEAN DEFAULT FALSE,
    auto_ban_threshold          INTEGER DEFAULT 350,
    auto_ban_duration_hours     INTEGER,
    min_reputation_to_join      INTEGER,
    sync_interval_minutes       INTEGER DEFAULT 5,
    notify_on_action            BOOLEAN DEFAULT TRUE,
    exempt_roles                TEXT[] DEFAULT '{}',
    sync_to_community           BOOLEAN DEFAULT FALSE,
    last_enforced_at            TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (server_config_id)
);

-- 7. Create server_access_log table
CREATE TABLE IF NOT EXISTS server_access_log (
    id                  SERIAL PRIMARY KEY,
    server_config_id    INTEGER NOT NULL REFERENCES server_status_configs(id) ON DELETE CASCADE,
    target_player       VARCHAR(255) NOT NULL,
    action              VARCHAR(30) NOT NULL
        CHECK (action IN (
            'whitelist_add', 'whitelist_remove',
            'auto_kick', 'auto_ban', 'auto_unban'
        )),
    reason              TEXT,
    reputation_score    INTEGER,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_server_access_log_server_time
    ON server_access_log (server_config_id, created_at DESC);
