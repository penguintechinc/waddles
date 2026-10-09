-- Migration 050: Add community premium status + seat limit, and global banner hub_settings

-- Community premium fields
ALTER TABLE communities
  ADD COLUMN IF NOT EXISTS is_premium BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS seat_limit INTEGER DEFAULT NULL;

COMMENT ON COLUMN communities.is_premium IS 'Super admin granted premium status — unlocks premium features for the community';
COMMENT ON COLUMN communities.seat_limit IS 'Optional max member count; NULL = unlimited';

-- Global announcement banner settings
-- hub_settings (000_create_base_schema) has no `description` column; add it first so the
-- seed INSERT below works on a fresh-DB replay (no-op where it already exists).
ALTER TABLE hub_settings ADD COLUMN IF NOT EXISTS description TEXT;

INSERT INTO hub_settings (setting_key, setting_value, description) VALUES
  ('banner_enabled',    'false',   'Show global announcement banner across all pages'),
  ('banner_text',       '',        'Banner message text (supports emoji and [text](url) links)'),
  ('banner_bg_color',   '#F5C518', 'Banner background colour (hex or CSS colour; default gold)'),
  ('banner_text_color', '#000000', 'Banner text/link colour (hex or CSS colour; default black)')
ON CONFLICT (setting_key) DO NOTHING;
