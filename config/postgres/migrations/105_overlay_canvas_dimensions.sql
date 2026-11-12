-- Migration 105: canvas dimensions on overlay tables --
-- Adds explicit canvas_resolution / canvas_width / canvas_height columns alongside
-- the canvas/theme details on community_overlay_tokens, presentation_config, and
-- overlay_surfaces so per-overlay canvas size (1080p, 2k, 4k) is a first-class
-- queryable field in the database.
--
-- Supported resolutions:
--   '1080p'  -> 1920 x 1080  (Full HD)
--   '2k'     -> 2560 x 1440  (QHD)
--   '4k'     -> 3840 x 2160  (Ultra HD)

ALTER TABLE community_overlay_tokens
    ADD COLUMN IF NOT EXISTS canvas_resolution  VARCHAR(8)   NOT NULL DEFAULT '1080p'
        CHECK (canvas_resolution IN ('1080p', '2k', '4k')),
    ADD COLUMN IF NOT EXISTS canvas_width       INTEGER      NOT NULL DEFAULT 1920
        CHECK (canvas_width  > 0 AND canvas_width  <= 3840),
    ADD COLUMN IF NOT EXISTS canvas_height      INTEGER      NOT NULL DEFAULT 1080
        CHECK (canvas_height > 0 AND canvas_height <= 2160);

UPDATE community_overlay_tokens
SET
    canvas_resolution = COALESCE(theme_config->>'resolution', '1080p'),
    canvas_width      = COALESCE((theme_config->>'width')::INTEGER,  1920),
    canvas_height     = COALESCE((theme_config->>'height')::INTEGER, 1080)
WHERE theme_config IS NOT NULL
  AND (theme_config->>'resolution' IS NOT NULL
    OR theme_config->>'width'      IS NOT NULL
    OR theme_config->>'height'     IS NOT NULL);

ALTER TABLE presentation_config
    ADD COLUMN IF NOT EXISTS canvas_resolution  VARCHAR(8)   NOT NULL DEFAULT '1080p'
        CHECK (canvas_resolution IN ('1080p', '2k', '4k')),
    ADD COLUMN IF NOT EXISTS canvas_width       INTEGER      NOT NULL DEFAULT 1920
        CHECK (canvas_width  > 0 AND canvas_width  <= 3840),
    ADD COLUMN IF NOT EXISTS canvas_height      INTEGER      NOT NULL DEFAULT 1080
        CHECK (canvas_height > 0 AND canvas_height <= 2160);

UPDATE presentation_config
SET
    canvas_resolution = COALESCE(config->>'resolution', '1080p'),
    canvas_width      = COALESCE((config->>'width')::INTEGER,  1920),
    canvas_height     = COALESCE((config->>'height')::INTEGER, 1080)
WHERE config IS NOT NULL
  AND (config->>'resolution' IS NOT NULL
    OR config->>'width'      IS NOT NULL
    OR config->>'height'     IS NOT NULL);

ALTER TABLE overlay_surfaces
    ADD COLUMN IF NOT EXISTS canvas_resolution  VARCHAR(8)   NOT NULL DEFAULT '1080p'
        CHECK (canvas_resolution IN ('1080p', '2k', '4k')),
    ADD COLUMN IF NOT EXISTS canvas_width       INTEGER      NOT NULL DEFAULT 1920
        CHECK (canvas_width  > 0 AND canvas_width  <= 3840),
    ADD COLUMN IF NOT EXISTS canvas_height      INTEGER      NOT NULL DEFAULT 1080
        CHECK (canvas_height > 0 AND canvas_height <= 2160);
