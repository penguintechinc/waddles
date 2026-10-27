-- Migration 012: Add Music Provider Configuration System
-- Description: Implements multi-provider music system with support for Spotify, YouTube, SoundCloud, Pretzel, Epidemic, StreamBeats, Monstercat, and Icecast
-- Author: Waddles Team
-- Date: 2025-12-15

-- =============================================================================
-- MUSIC PROVIDER CONFIG TABLE
-- =============================================================================
-- Stores OAuth tokens and configuration for each music provider per community
-- Supports multiple providers with selective enablement
CREATE TABLE IF NOT EXISTS music_provider_config (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    provider_type VARCHAR(50) NOT NULL CHECK (provider_type IN ('spotify', 'youtube', 'soundcloud', 'pretzel', 'epidemic', 'streambeats', 'monstercat', 'icecast')),
    is_enabled BOOLEAN DEFAULT FALSE,
    oauth_access_token TEXT,                              -- Encrypted by application
    oauth_refresh_token TEXT,                             -- Encrypted by application
    oauth_expires_at TIMESTAMPTZ,
    config JSONB DEFAULT '{}',                            -- Provider-specific configuration
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(community_id, provider_type)
);

COMMENT ON TABLE music_provider_config IS
    'Configuration for music providers per community. Stores OAuth tokens and provider-specific settings.';

COMMENT ON COLUMN music_provider_config.community_id IS
    'Community that owns this provider configuration';

COMMENT ON COLUMN music_provider_config.provider_type IS
    'Type of music provider: spotify, youtube, soundcloud, pretzel, epidemic, streambeats, monstercat, icecast';

COMMENT ON COLUMN music_provider_config.is_enabled IS
    'Whether this provider is currently enabled for the community';

COMMENT ON COLUMN music_provider_config.oauth_access_token IS
    'OAuth access token (encrypted at application level)';

COMMENT ON COLUMN music_provider_config.oauth_refresh_token IS
    'OAuth refresh token for token renewal (encrypted at application level)';

COMMENT ON COLUMN music_provider_config.oauth_expires_at IS
    'When the OAuth access token expires';

COMMENT ON COLUMN music_provider_config.config IS
    'Provider-specific configuration (e.g., playlist IDs, API keys, stream URLs)';

-- Index for provider lookups
CREATE INDEX IF NOT EXISTS idx_music_provider_config_lookup
ON music_provider_config(community_id, provider_type);

-- Index for enabled providers query
CREATE INDEX IF NOT EXISTS idx_music_provider_config_enabled
ON music_provider_config(community_id, is_enabled)
WHERE is_enabled = TRUE;

-- Index for token expiration cleanup
CREATE INDEX IF NOT EXISTS idx_music_provider_config_expiry
ON music_provider_config(oauth_expires_at)
WHERE oauth_expires_at IS NOT NULL;

COMMENT ON INDEX idx_music_provider_config_lookup IS
    'Efficient lookup of provider configuration by community and provider type';

COMMENT ON INDEX idx_music_provider_config_enabled IS
    'Efficient lookup of enabled providers for a community';

COMMENT ON INDEX idx_music_provider_config_expiry IS
    'Efficient identification of expired OAuth tokens for renewal';

-- =============================================================================
-- MUSIC RADIO STATE TABLE
-- =============================================================================
-- Tracks current radio mode and streaming state for each community
CREATE TABLE IF NOT EXISTS music_radio_state (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE UNIQUE,
    mode VARCHAR(20) DEFAULT 'music' CHECK (mode IN ('music', 'radio')),
    current_station_url VARCHAR(500),
    current_station_name VARCHAR(255),
    stream_metadata JSONB DEFAULT '{}',                   -- Current stream info (bitrate, codec, etc.)
    started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE music_radio_state IS
    'Tracks the current radio/music mode state and streaming information for each community';

COMMENT ON COLUMN music_radio_state.community_id IS
    'Community that owns this radio state';

COMMENT ON COLUMN music_radio_state.mode IS
    'Current mode: music (song-based) or radio (streaming)';

COMMENT ON COLUMN music_radio_state.current_station_url IS
    'URL of the currently playing radio station (only for radio mode)';

COMMENT ON COLUMN music_radio_state.current_station_name IS
    'Name of the currently playing radio station';

COMMENT ON COLUMN music_radio_state.stream_metadata IS
    'Current stream metadata (e.g., {bitrate: 128, codec: "mp3", genre: "Electronic"}';

COMMENT ON COLUMN music_radio_state.started_at IS
    'When the current stream/station started playing';

-- Index for state lookups
CREATE INDEX IF NOT EXISTS idx_music_radio_state_lookup
ON music_radio_state(community_id);

-- Index for updated_at for monitoring active streams
CREATE INDEX IF NOT EXISTS idx_music_radio_state_updated
ON music_radio_state(updated_at DESC);

COMMENT ON INDEX idx_music_radio_state_lookup IS
    'Efficient lookup of radio state by community';

COMMENT ON INDEX idx_music_radio_state_updated IS
    'Efficient lookup of recently updated streams';

-- =============================================================================
-- MUSIC QUEUE TABLE
-- =============================================================================
-- Manages song requests and queue state for music mode
CREATE TABLE IF NOT EXISTS music_queue (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    provider VARCHAR(50) NOT NULL,                        -- Provider where track is from
    track_id VARCHAR(255) NOT NULL,                       -- Provider-specific track identifier
    track_name VARCHAR(500),
    artist_name VARCHAR(500),
    album_art_url VARCHAR(500),
    duration_ms INTEGER,
    requested_by_user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    votes INTEGER DEFAULT 0,
    position INTEGER,                                     -- Position in queue (0 = now playing)
    status VARCHAR(20) DEFAULT 'queued' CHECK (status IN ('queued', 'playing', 'played', 'skipped')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE music_queue IS
    'Music queue for song-based providers. Tracks requested tracks and their playback status.';

COMMENT ON COLUMN music_queue.community_id IS
    'Community that owns this queue item';

COMMENT ON COLUMN music_queue.provider IS
    'Music provider where this track is from (e.g., spotify, youtube)';

COMMENT ON COLUMN music_queue.track_id IS
    'Unique identifier for the track in the provider system';

COMMENT ON COLUMN music_queue.track_name IS
    'Display name of the track';

COMMENT ON COLUMN music_queue.artist_name IS
    'Primary artist for the track';

COMMENT ON COLUMN music_queue.album_art_url IS
    'URL to album artwork thumbnail';

COMMENT ON COLUMN music_queue.duration_ms IS
    'Track duration in milliseconds';

COMMENT ON COLUMN music_queue.requested_by_user_id IS
    'User who requested this track';

COMMENT ON COLUMN music_queue.votes IS
    'Current vote count (for vote-based prioritization)';

COMMENT ON COLUMN music_queue.position IS
    'Position in queue: 0 = now playing, 1+ = upcoming';

COMMENT ON COLUMN music_queue.status IS
    'Current status: queued, playing, played, or skipped';

-- Index for queue lookups
CREATE INDEX IF NOT EXISTS idx_music_queue_community
ON music_queue(community_id, position, created_at ASC);

-- Index for current playing track
CREATE INDEX IF NOT EXISTS idx_music_queue_now_playing
ON music_queue(community_id, status)
WHERE status IN ('playing', 'queued');

-- Index for user requests
CREATE INDEX IF NOT EXISTS idx_music_queue_user
ON music_queue(requested_by_user_id, community_id);

-- Index for queued items
CREATE INDEX IF NOT EXISTS idx_music_queue_pending
ON music_queue(community_id, votes DESC, created_at ASC)
WHERE status = 'queued';

-- Index for history cleanup
CREATE INDEX IF NOT EXISTS idx_music_queue_history
ON music_queue(community_id, created_at DESC)
WHERE status IN ('played', 'skipped');

COMMENT ON INDEX idx_music_queue_community IS
    'Efficient retrieval of queue items in order';

COMMENT ON INDEX idx_music_queue_now_playing IS
    'Quick lookup of current/next playing items';

COMMENT ON INDEX idx_music_queue_user IS
    'Find all requests from a specific user';

COMMENT ON INDEX idx_music_queue_pending IS
    'Sort pending requests by votes and request time';

COMMENT ON INDEX idx_music_queue_history IS
    'Find played/skipped items for history display';

-- =============================================================================
-- HELPER FUNCTIONS
-- =============================================================================

-- Function to get enabled providers for a community
CREATE OR REPLACE FUNCTION get_enabled_providers(p_community_id INTEGER)
RETURNS TABLE(provider_type VARCHAR, config JSONB) AS $$
BEGIN
    RETURN QUERY
    SELECT mpc.provider_type, mpc.config
    FROM music_provider_config mpc
    WHERE mpc.community_id = p_community_id
      AND mpc.is_enabled = TRUE;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_enabled_providers(INTEGER) IS
    'Get all enabled music providers for a community';

-- Function to get current queue for a community
CREATE OR REPLACE FUNCTION get_music_queue(p_community_id INTEGER, p_limit INTEGER DEFAULT 10)
RETURNS TABLE(
    id INTEGER,
    provider VARCHAR,
    track_name VARCHAR,
    artist_name VARCHAR,
    album_art_url VARCHAR,
    duration_ms INTEGER,
    requested_by_user_id INTEGER,
    votes INTEGER,
    "position" INTEGER,
    status VARCHAR
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        mq.id,
        mq.provider,
        mq.track_name,
        mq.artist_name,
        mq.album_art_url,
        mq.duration_ms,
        mq.requested_by_user_id,
        mq.votes,
        mq.position,
        mq.status
    FROM music_queue mq
    WHERE mq.community_id = p_community_id
      AND mq.status IN ('queued', 'playing')
    ORDER BY mq.position ASC, mq.votes DESC, mq.created_at ASC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_music_queue(INTEGER, INTEGER) IS
    'Get the current music queue for a community, ordered by position and votes';

-- Function to refresh provider token
CREATE OR REPLACE FUNCTION refresh_provider_token(
    p_community_id INTEGER,
    p_provider_type VARCHAR,
    p_new_access_token TEXT,
    p_new_refresh_token TEXT,
    p_expires_at TIMESTAMPTZ
)
RETURNS BOOLEAN AS $$
BEGIN
    UPDATE music_provider_config
    SET
        oauth_access_token = p_new_access_token,
        oauth_refresh_token = COALESCE(p_new_refresh_token, oauth_refresh_token),
        oauth_expires_at = p_expires_at,
        updated_at = NOW()
    WHERE community_id = p_community_id
      AND provider_type = p_provider_type;

    RETURN FOUND;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION refresh_provider_token(INTEGER, VARCHAR, TEXT, TEXT, TIMESTAMPTZ) IS
    'Update OAuth tokens for a provider configuration';

-- Function to transition queue to playing and cleanup history
CREATE OR REPLACE FUNCTION advance_music_queue(p_community_id INTEGER)
RETURNS TABLE(
    previous_track_id INTEGER,
    next_track_id INTEGER
) AS $$
DECLARE
    v_current_track INTEGER;
    v_next_track INTEGER;
BEGIN
    -- Mark currently playing as played
    UPDATE music_queue
    SET status = 'played'
    WHERE community_id = p_community_id
      AND status = 'playing'
    RETURNING id INTO v_current_track;

    -- Move next queued item to playing
    UPDATE music_queue
    SET status = 'playing', position = 0
    WHERE id = (
        SELECT id FROM music_queue
        WHERE community_id = p_community_id
          AND status = 'queued'
        ORDER BY position ASC, votes DESC, created_at ASC
        LIMIT 1
    )
    RETURNING id INTO v_next_track;

    -- Update positions for remaining queue items
    WITH ranked_queue AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY votes DESC, created_at ASC) as new_position
        FROM music_queue
        WHERE community_id = p_community_id
          AND status = 'queued'
    )
    UPDATE music_queue
    SET position = ranked_queue.new_position
    FROM ranked_queue
    WHERE music_queue.id = ranked_queue.id;

    previous_track_id := v_current_track;
    next_track_id := v_next_track;
    RETURN NEXT;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION advance_music_queue(INTEGER) IS
    'Move current playing track to history and promote next queued track to playing';

-- Function to add vote to queue item
CREATE OR REPLACE FUNCTION vote_music_queue_item(p_queue_id INTEGER, p_votes_to_add INTEGER DEFAULT 1)
RETURNS INTEGER AS $$
DECLARE
    v_new_votes INTEGER;
BEGIN
    UPDATE music_queue
    SET votes = votes + p_votes_to_add
    WHERE id = p_queue_id
    RETURNING votes INTO v_new_votes;

    RETURN v_new_votes;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION vote_music_queue_item(INTEGER, INTEGER) IS
    'Add votes to a music queue item and return new vote count';

-- =============================================================================
-- ANALYZE TABLES
-- =============================================================================
ANALYZE music_provider_config;
ANALYZE music_radio_state;
ANALYZE music_queue;
