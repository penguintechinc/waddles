-- Migration 018: Add Simple Games Tables
-- Description: Implements simple luck-based games (Dice, RPS, 8-Ball)
--              with betting, cooldown tracking, and statistics
-- Author: Waddles Team
-- Date: 2025-12-16

-- =============================================================================
-- 1. SIMPLE GAME RESULTS TABLE
-- =============================================================================
-- Records all simple game (dice, rps, eightball) results and statistics
CREATE TABLE IF NOT EXISTS loyalty_simple_game_results (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    hub_user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL,
    platform VARCHAR(50) NOT NULL,                          -- twitch, youtube, discord, etc.
    platform_user_id VARCHAR(255) NOT NULL,
    game_type VARCHAR(20) NOT NULL CHECK (game_type IN ('dice', 'rps', 'eightball')),
    bet_amount INTEGER NOT NULL DEFAULT 0,                  -- 0 for non-wagered games (eightball)
    win_amount INTEGER NOT NULL DEFAULT 0,
    result_data JSONB NOT NULL,                             -- Game-specific data (roll, choice, response, etc.)
    is_win BOOLEAN,                                         -- NULL for non-wagered games (eightball)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE loyalty_simple_game_results IS
    'Records all simple game results (Dice, RPS, 8-Ball) for statistics and analytics';

COMMENT ON COLUMN loyalty_simple_game_results.community_id IS
    'Community where the game was played';

COMMENT ON COLUMN loyalty_simple_game_results.hub_user_id IS
    'Hub user ID if linked (may be NULL for unlinked platform users)';

COMMENT ON COLUMN loyalty_simple_game_results.platform IS
    'Gaming platform (twitch, youtube, discord, etc.)';

COMMENT ON COLUMN loyalty_simple_game_results.platform_user_id IS
    'Platform-specific user identifier';

COMMENT ON COLUMN loyalty_simple_game_results.game_type IS
    'Type of game: dice, rps (rock-paper-scissors), or eightball';

COMMENT ON COLUMN loyalty_simple_game_results.bet_amount IS
    'Amount wagered (0 for non-wagered games like eightball)';

COMMENT ON COLUMN loyalty_simple_game_results.win_amount IS
    'Amount won (0 if lost or non-wagered game)';

COMMENT ON COLUMN loyalty_simple_game_results.result_data IS
    'Game-specific result data (e.g., {roll: 5, multiplier: 1.5} for dice)';

COMMENT ON COLUMN loyalty_simple_game_results.is_win IS
    'Whether player won (NULL for non-wagered games)';

-- Indexes for loyalty_simple_game_results
CREATE INDEX IF NOT EXISTS idx_simple_games_community
ON loyalty_simple_game_results(community_id, created_at DESC);

COMMENT ON INDEX idx_simple_games_community IS
    'Efficient lookup of games for a community in chronological order';

CREATE INDEX IF NOT EXISTS idx_simple_games_user
ON loyalty_simple_game_results(community_id, platform, platform_user_id, created_at DESC);

COMMENT ON INDEX idx_simple_games_user IS
    'Efficient lookup of games for a user';

CREATE INDEX IF NOT EXISTS idx_simple_games_type
ON loyalty_simple_game_results(community_id, game_type, created_at DESC);

COMMENT ON INDEX idx_simple_games_type IS
    'Efficient lookup of games by type for a community';

CREATE INDEX IF NOT EXISTS idx_simple_games_user_type
ON loyalty_simple_game_results(community_id, platform, platform_user_id, game_type, created_at DESC);

COMMENT ON INDEX idx_simple_games_user_type IS
    'Efficient lookup of specific game type for a user';

CREATE INDEX IF NOT EXISTS idx_simple_games_wins
ON loyalty_simple_game_results(community_id, game_type)
WHERE is_win = TRUE;

COMMENT ON INDEX idx_simple_games_wins IS
    'Efficient lookup of winning games for statistics';

-- =============================================================================
-- 2. SIMPLE GAME COOLDOWNS TABLE
-- =============================================================================
-- Tracks user cooldowns per game to prevent spam
CREATE TABLE IF NOT EXISTS loyalty_simple_game_cooldowns (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    platform_user_id VARCHAR(255) NOT NULL,
    game_type VARCHAR(20) NOT NULL CHECK (game_type IN ('dice', 'rps', 'eightball')),
    cooldown_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(community_id, platform, platform_user_id, game_type)
);

COMMENT ON TABLE loyalty_simple_game_cooldowns IS
    'Tracks user cooldowns for each simple game to prevent spam and balance gameplay';

COMMENT ON COLUMN loyalty_simple_game_cooldowns.community_id IS
    'Community context';

COMMENT ON COLUMN loyalty_simple_game_cooldowns.platform IS
    'User platform';

COMMENT ON COLUMN loyalty_simple_game_cooldowns.platform_user_id IS
    'Platform-specific user ID';

COMMENT ON COLUMN loyalty_simple_game_cooldowns.game_type IS
    'Game type with cooldown (dice: 15s, rps: 20s, eightball: 10s)';

COMMENT ON COLUMN loyalty_simple_game_cooldowns.cooldown_until IS
    'Timestamp when cooldown expires';

-- Indexes for loyalty_simple_game_cooldowns
CREATE INDEX IF NOT EXISTS idx_simple_cooldowns_active
ON loyalty_simple_game_cooldowns(community_id, platform, platform_user_id, game_type);

COMMENT ON INDEX idx_simple_cooldowns_active IS
    'Efficient lookup of active cooldowns for a user';

CREATE INDEX IF NOT EXISTS idx_simple_cooldowns_expired
ON loyalty_simple_game_cooldowns(community_id);

COMMENT ON INDEX idx_simple_cooldowns_expired IS
    'Efficient cleanup of expired cooldowns';

-- =============================================================================
-- 3. GAME CONFIGURATION EXTENSIONS
-- =============================================================================
-- Fresh-DB replay: loyalty_config is created by alembic 0015_loyalty_core_tables.py,
-- which runs AFTER this baseline file, so on an empty DB the ALTER below silently
-- did nothing and the COMMENTs failed the whole file. Create it first with the
-- identical DDL (IF NOT EXISTS -> no-op on DBs that already have it; 0015 is then a
-- no-op too) so the simple-games columns land on fresh databases as well.
CREATE TABLE IF NOT EXISTS loyalty_config (
    id SERIAL PRIMARY KEY,
    community_id INTEGER NOT NULL UNIQUE
        REFERENCES communities(id) ON DELETE CASCADE,
    currency_name VARCHAR(50) NOT NULL DEFAULT 'Points',
    currency_symbol VARCHAR(16) NOT NULL DEFAULT '🪙',
    earn_chat_points INTEGER NOT NULL DEFAULT 1,
    earn_chat_cooldown_s INTEGER NOT NULL DEFAULT 60,
    earn_watch_points_per_min INTEGER NOT NULL DEFAULT 1,
    earn_watch_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    max_balance BIGINT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Add simple games columns to loyalty_config if not already present
ALTER TABLE IF EXISTS loyalty_config
    ADD COLUMN IF NOT EXISTS simple_games_enabled BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS dice_min_bet INTEGER DEFAULT 10,
    ADD COLUMN IF NOT EXISTS dice_max_bet INTEGER DEFAULT 500,
    ADD COLUMN IF NOT EXISTS rps_min_bet INTEGER DEFAULT 10,
    ADD COLUMN IF NOT EXISTS rps_max_bet INTEGER DEFAULT 500;

COMMENT ON COLUMN loyalty_config.simple_games_enabled IS
    'Whether simple games are enabled for this community';

COMMENT ON COLUMN loyalty_config.dice_min_bet IS
    'Minimum bet amount for dice game';

COMMENT ON COLUMN loyalty_config.dice_max_bet IS
    'Maximum bet amount for dice game';

COMMENT ON COLUMN loyalty_config.rps_min_bet IS
    'Minimum bet amount for rock-paper-scissors game';

COMMENT ON COLUMN loyalty_config.rps_max_bet IS
    'Maximum bet amount for rock-paper-scissors game';

-- =============================================================================
-- 4. TRIGGER FUNCTIONS FOR AUTOMATIC TIMESTAMP UPDATES
-- =============================================================================

-- Trigger for loyalty_simple_game_results
DROP TRIGGER IF EXISTS trigger_simple_game_results_updated_at ON loyalty_simple_game_results;
CREATE TRIGGER trigger_simple_game_results_updated_at
    BEFORE UPDATE ON loyalty_simple_game_results
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TRIGGER trigger_simple_game_results_updated_at ON loyalty_simple_game_results IS
    'Automatically update updated_at timestamp on result modifications';

-- Trigger for loyalty_simple_game_cooldowns
DROP TRIGGER IF EXISTS trigger_simple_game_cooldowns_updated_at ON loyalty_simple_game_cooldowns;
CREATE TRIGGER trigger_simple_game_cooldowns_updated_at
    BEFORE UPDATE ON loyalty_simple_game_cooldowns
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TRIGGER trigger_simple_game_cooldowns_updated_at ON loyalty_simple_game_cooldowns IS
    'Automatically update updated_at timestamp on cooldown modifications';

-- =============================================================================
-- 5. HELPER FUNCTIONS FOR SIMPLE GAMES
-- =============================================================================

-- Function to get simple game statistics for a user
CREATE OR REPLACE FUNCTION get_simple_game_stats(
    p_community_id INTEGER,
    p_platform VARCHAR,
    p_platform_user_id VARCHAR,
    p_game_type VARCHAR DEFAULT NULL
)
RETURNS TABLE(
    game_type VARCHAR,
    total_games INTEGER,
    wins INTEGER,
    losses INTEGER,
    win_rate NUMERIC,
    total_bet BIGINT,
    total_won BIGINT,
    net BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        lsgr.game_type,
        COUNT(*)::INTEGER,
        SUM(CASE WHEN lsgr.is_win = TRUE THEN 1 ELSE 0 END)::INTEGER,
        SUM(CASE WHEN lsgr.is_win = FALSE THEN 1 ELSE 0 END)::INTEGER,
        CASE
            WHEN COUNT(*) = 0 THEN 0::NUMERIC
            ELSE (SUM(CASE WHEN lsgr.is_win = TRUE THEN 1 ELSE 0 END)::NUMERIC / COUNT(*)::NUMERIC * 100)
        END,
        SUM(lsgr.bet_amount),
        SUM(lsgr.win_amount),
        SUM(lsgr.win_amount) - SUM(lsgr.bet_amount)
    FROM loyalty_simple_game_results lsgr
    WHERE lsgr.community_id = p_community_id
      AND lsgr.platform = p_platform
      AND lsgr.platform_user_id = p_platform_user_id
      AND (p_game_type IS NULL OR lsgr.game_type = p_game_type)
    GROUP BY lsgr.game_type;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_simple_game_stats(INTEGER, VARCHAR, VARCHAR, VARCHAR) IS
    'Get simple game statistics for a user (optionally filtered by game type)';

-- Function to get leaderboard for a simple game
CREATE OR REPLACE FUNCTION get_simple_game_leaderboard(
    p_community_id INTEGER,
    p_game_type VARCHAR,
    p_limit INTEGER DEFAULT 10
)
RETURNS TABLE(
    platform VARCHAR,
    platform_user_id VARCHAR,
    total_games INTEGER,
    wins INTEGER,
    losses INTEGER,
    win_rate NUMERIC,
    total_won BIGINT,
    net BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        lsgr.platform,
        lsgr.platform_user_id,
        COUNT(*)::INTEGER,
        SUM(CASE WHEN lsgr.is_win = TRUE THEN 1 ELSE 0 END)::INTEGER,
        SUM(CASE WHEN lsgr.is_win = FALSE THEN 1 ELSE 0 END)::INTEGER,
        CASE
            WHEN COUNT(*) = 0 THEN 0::NUMERIC
            ELSE (SUM(CASE WHEN lsgr.is_win = TRUE THEN 1 ELSE 0 END)::NUMERIC / COUNT(*)::NUMERIC * 100)
        END,
        SUM(lsgr.win_amount),
        SUM(lsgr.win_amount) - SUM(lsgr.bet_amount)
    FROM loyalty_simple_game_results lsgr
    WHERE lsgr.community_id = p_community_id
      AND lsgr.game_type = p_game_type
    GROUP BY lsgr.platform, lsgr.platform_user_id
    ORDER BY SUM(CASE WHEN lsgr.is_win = TRUE THEN 1 ELSE 0 END) DESC, COUNT(*) DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_simple_game_leaderboard(INTEGER, VARCHAR, INTEGER) IS
    'Get leaderboard for a specific simple game type';

-- Function to cleanup expired cooldowns (can be run as periodic maintenance)
CREATE OR REPLACE FUNCTION cleanup_expired_cooldowns()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM loyalty_simple_game_cooldowns
    WHERE cooldown_until <= NOW();

    GET DIAGNOSTICS deleted_count = ROW_COUNT;

    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION cleanup_expired_cooldowns() IS
    'Remove expired cooldowns from the database (safe for periodic cleanup jobs)';

-- =============================================================================
-- 6. ANALYZE TABLES FOR QUERY OPTIMIZATION
-- =============================================================================

ANALYZE loyalty_simple_game_results;
ANALYZE loyalty_simple_game_cooldowns;

-- =============================================================================
-- 7. MIGRATION SUMMARY AND DOCUMENTATION
-- =============================================================================

/*
MIGRATION 018: SIMPLE GAMES SYSTEM - SUMMARY
==============================================

This migration implements three simple luck-based games with betting and statistics:

1. SIMPLE GAME RESULTS TABLE
   - Tracks all game plays across three types: dice, rps (rock-paper-scissors), eightball
   - Stores community context, player identifiers, bet amounts, winnings, and game-specific data
   - Supports both wagered (dice, rps) and non-wagered (eightball) games
   - Result data stored as JSONB for flexible game-specific information
   - 5 indexes for efficient querying by community, user, and game type

2. SIMPLE GAME COOLDOWNS TABLE
   - Per-user, per-game cooldowns to prevent spam and balance gameplay
   - Configurable cooldowns: Dice 15s, RPS 20s, 8-Ball 10s
   - Automatic tracking with expiration timestamps
   - 2 indexes for active cooldown queries and cleanup

3. GAME CONFIGURATION EXTENSIONS
   - Added to existing loyalty_config table
   - Configurable min/max bets per game (dice and rps)
   - Master enable/disable flag for simple games feature

4. GAMES IMPLEMENTED

   DICE GAME (!dice [bet])
   - Roll 1-6 RNG
   - Win on 4-6 (66% win rate)
   - Payout: 1.5x bet on win
   - Cooldown: 15 seconds
   - Minimum house edge: 33% win rate but 1.5x doesn't cover expected value perfectly

   RPS GAME (!rps [rock/paper/scissors] [bet])
   - Rock-Paper-Scissors vs bot
   - Bot chooses randomly from: rock, paper, scissors
   - Win conditions: rock beats scissors, paper beats rock, scissors beats paper
   - Payout: 2x bet on win (double or nothing)
   - Tie: Automatic refund of bet
   - Cooldown: 20 seconds
   - House edge: 33% (1/3 win, 1/3 tie, 1/3 loss with 2x payout = slight player advantage)

   8-BALL GAME (!8ball [question])
   - Magic 8-ball fortune teller
   - No betting, purely for entertainment
   - 20 varied responses (10 positive, 5 non-committal, 5 negative)
   - Cooldown: 10 seconds
   - No currency transactions

5. HELPER FUNCTIONS (3 comprehensive functions)
   - get_simple_game_stats: Get statistics for a user/game
   - get_simple_game_leaderboard: Get top players for a game type
   - cleanup_expired_cooldowns: Maintenance function to remove expired cooldowns

6. AUTOMATIC TIMESTAMP MANAGEMENT
   - Updated_at triggers on both result and cooldown tables
   - Ensures accurate modification tracking

7. DATA INTEGRITY
   - Foreign key constraints with ON DELETE CASCADE
   - UNIQUE constraints for single-entry cooldowns
   - CHECK constraints for game_type validation
   - Result data as JSONB for extensibility

8. PERFORMANCE OPTIMIZATION
   - 7 strategic indexes across 2 tables
   - Composite indexes for multi-column queries
   - Filtered indexes for active records
   - ANALYZE statements for query planner optimization

USAGE PATTERNS:

-- Get user's dice statistics
SELECT * FROM get_simple_game_stats(community_id, 'twitch', user_id, 'dice');

-- Get leaderboard for RPS
SELECT * FROM get_simple_game_leaderboard(community_id, 'rps', 10);

-- Check active cooldowns
SELECT * FROM loyalty_simple_game_cooldowns
WHERE community_id = ? AND platform = ? AND platform_user_id = ?
AND cooldown_until > NOW();

-- Get overall statistics
SELECT game_type, COUNT(*) as total,
       SUM(CASE WHEN is_win THEN 1 ELSE 0 END) as wins
FROM loyalty_simple_game_results
WHERE community_id = ? AND platform = ? AND platform_user_id = ?
GROUP BY game_type;

CONFIGURATION:
-- Enable/disable simple games
UPDATE loyalty_config SET simple_games_enabled = FALSE
WHERE community_id = ?;

-- Configure dice bets
UPDATE loyalty_config SET dice_min_bet = 50, dice_max_bet = 1000
WHERE community_id = ?;

-- Configure RPS bets
UPDATE loyalty_config SET rps_min_bet = 25, rps_max_bet = 500
WHERE community_id = ?;

DATABASE SCHEMA RELATIONSHIPS:
- loyalty_simple_game_results -> communities (via community_id)
- loyalty_simple_game_results -> hub_users (via hub_user_id, optional)
- loyalty_simple_game_cooldowns -> communities (via community_id)

BALANCE AND ODDS:
Dice (66% win rate): Expected value = 0.66 * 1.5x - 0.34 * 1x = 0.66x (66% payout)
RPS (33.33% win): 0.33 * 2x - 0.33 * 1x + 0.33 refund = ~0.33x (33% payout + tie)
8-Ball: 0% house take (entertainment only)

*/

-- =============================================================================
-- Migration 018 Complete
-- =============================================================================
