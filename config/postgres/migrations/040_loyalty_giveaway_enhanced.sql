-- Migration 040: Loyalty Giveaway Enhancements
-- Adds game key giveaways, multi-winner support, and eligibility gating
-- Depends on: loyalty_giveaways (from loyalty module migrations)
--
-- Fresh-DB replay: loyalty_giveaways has no CREATE TABLE in ANY migration chain (the
-- loyalty module was expected to provision it) so on an empty database this file
-- cannot apply. The whole body is guarded -- a WARNING, not a silent skip -- until a
-- migration actually provisions the base table. Already-migrated DBs have this file
-- recorded as applied and never re-run it.

DO $mig$
BEGIN
  IF to_regclass('public.loyalty_giveaways') IS NULL THEN
    RAISE WARNING '040: loyalty_giveaways does not exist (no migration creates it); giveaway enhancements not applied';
    RETURN;
  END IF;

  -- ============================================================================
  -- ALTER loyalty_giveaways — add new giveaway types and eligibility fields
  -- ============================================================================
  ALTER TABLE loyalty_giveaways
      ADD COLUMN IF NOT EXISTS giveaway_type VARCHAR(20) NOT NULL DEFAULT 'standard',
      ADD COLUMN IF NOT EXISTS sub_only BOOLEAN DEFAULT FALSE,
      ADD COLUMN IF NOT EXISTS min_account_age_days INTEGER DEFAULT 0,
      ADD COLUMN IF NOT EXISTS loyalty_threshold INTEGER DEFAULT 0,
      ADD COLUMN IF NOT EXISTS winner_count INTEGER DEFAULT 1,
      ADD COLUMN IF NOT EXISTS key_platform VARCHAR(50),
      ADD COLUMN IF NOT EXISTS notification_message TEXT;

  -- Validate giveaway_type values
  ALTER TABLE loyalty_giveaways
      DROP CONSTRAINT IF EXISTS chk_giveaway_type;
  ALTER TABLE loyalty_giveaways
      ADD CONSTRAINT chk_giveaway_type
      CHECK (giveaway_type IN ('standard', 'game_key', 'multi_winner'));


  -- ============================================================================
  -- Loyalty Giveaway Keys — stores game keys for key-type giveaways
  -- Keys are encrypted at rest; is_valid allows admin to invalidate
  -- ============================================================================
  CREATE TABLE IF NOT EXISTS loyalty_giveaway_keys (
      id SERIAL PRIMARY KEY,
      giveaway_id INTEGER NOT NULL REFERENCES loyalty_giveaways(id) ON DELETE CASCADE,
      key_value VARCHAR(500) NOT NULL,
      key_platform VARCHAR(50),  -- steam, epic, gog, etc.
      is_valid BOOLEAN DEFAULT TRUE,
      is_claimed BOOLEAN DEFAULT FALSE,
      claimed_by_user_id VARCHAR(100),
      claimed_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT NOW()
  );

  CREATE INDEX IF NOT EXISTS idx_giveaway_keys_giveaway
      ON loyalty_giveaway_keys (giveaway_id);

  CREATE INDEX IF NOT EXISTS idx_giveaway_keys_unclaimed
      ON loyalty_giveaway_keys (giveaway_id)
      WHERE is_claimed = FALSE AND is_valid = TRUE;


  -- ============================================================================
  -- Loyalty Giveaway Winners — tracks multiple winners per giveaway
  -- Links winner slots to keys for key-type giveaways
  -- ============================================================================
  CREATE TABLE IF NOT EXISTS loyalty_giveaway_winners (
      id SERIAL PRIMARY KEY,
      giveaway_id INTEGER NOT NULL REFERENCES loyalty_giveaways(id) ON DELETE CASCADE,
      winner_number INTEGER NOT NULL,
      user_id VARCHAR(100) NOT NULL,
      platform VARCHAR(50),
      key_id INTEGER REFERENCES loyalty_giveaway_keys(id) ON DELETE SET NULL,
      notified BOOLEAN DEFAULT FALSE,
      notified_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ DEFAULT NOW(),

      CONSTRAINT uq_giveaway_winner_number
          UNIQUE (giveaway_id, winner_number)
  );

  CREATE INDEX IF NOT EXISTS idx_giveaway_winners_giveaway
      ON loyalty_giveaway_winners (giveaway_id);

  CREATE INDEX IF NOT EXISTS idx_giveaway_winners_user
      ON loyalty_giveaway_winners (user_id);

END
$mig$;
