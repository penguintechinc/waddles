-- Migration 102: caption_events for svc-presentation-rust's caption overlay
-- (the Rust port of core/browser_source_core_module's `/overlay/captions/
-- <key>` + `/ws/captions/<community_id>` + `POST /api/v1/internal/captions`
-- trio).
--
-- `caption_events` already exists from migration 007 (written by the Python
-- browser_source module until the parity cutover retires it). This migration
-- is therefore written to be correct in BOTH situations, never destructive of
-- rows the Python path still needs:
--   * fresh database (007 not replayed, or replay failed -- see the known
--     fresh-replay gaps in the 000-096 chain): CREATE TABLE IF NOT EXISTS
--     builds the table in its final shape;
--   * existing database (the normal case): the ALTERs below reconcile the
--     007 shape with the column TYPES the Rust SeaORM entity decodes
--     (`src/db/entities/caption_event.rs`). sqlx is strict about SQL/Rust
--     type agreement -- decoding an INT4 `community_id` into an `i64`, or a
--     NUMERIC `confidence_score` into an `f64`, is a hard runtime error, not
--     a silent coercion -- so every mismatching column is widened here.
--     Every ALTER is a no-op when the column is already in its target shape,
--     so re-running this migration is safe.
--
-- PII (critical-rules.md PII Tokenization): the Rust service never writes a
-- username. It stores `user_ref`, the tenant-tokenized UUID of the author,
-- and nothing that names them; a reconnecting overlay client therefore sees
-- replayed captions without an attribution name (the live display name only
-- ever exists in the push that carried it, never at rest here). The legacy
-- `username` column stays ONLY so the Python module's INSERT keeps working
-- until it is decommissioned -- it is made nullable below (Rust never fills
-- it) and is dropped by the cutover migration that retires the Python
-- caption path.

CREATE TABLE IF NOT EXISTS caption_events (
    id                  BIGSERIAL PRIMARY KEY,
    community_id        BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    user_ref            UUID,
    platform            VARCHAR(50) NOT NULL,
    original_message    TEXT NOT NULL,
    translated_message  TEXT,
    detected_language   VARCHAR(10),
    target_language     VARCHAR(10),
    confidence_score    DOUBLE PRECISION,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reconcile an existing 007-shaped table with the Rust entity's types.
ALTER TABLE caption_events ADD COLUMN IF NOT EXISTS user_ref UUID;

ALTER TABLE caption_events ALTER COLUMN id TYPE BIGINT;
ALTER SEQUENCE IF EXISTS caption_events_id_seq AS BIGINT;

ALTER TABLE caption_events ALTER COLUMN community_id TYPE BIGINT;
-- A row with no community can never be shown to anyone (every read is
-- community-scoped), so removing such orphans loses nothing observable and
-- lets the tenant-scoping column be NOT NULL.
DELETE FROM caption_events WHERE community_id IS NULL;
ALTER TABLE caption_events ALTER COLUMN community_id SET NOT NULL;

UPDATE caption_events SET created_at = NOW() WHERE created_at IS NULL;
ALTER TABLE caption_events ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE caption_events ALTER COLUMN created_at SET NOT NULL;

ALTER TABLE caption_events
    ALTER COLUMN confidence_score TYPE DOUBLE PRECISION
    USING confidence_score::double precision;

-- Legacy PII column (007): keep it for the Python writer until cutover, but
-- never require it -- the Rust service does not populate it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'caption_events'
          AND column_name = 'username'
    ) THEN
        ALTER TABLE caption_events ALTER COLUMN username DROP NOT NULL;
        COMMENT ON COLUMN caption_events.username IS
            'DEPRECATED legacy PII column, written only by the Python browser_source module; dropped at the Python caption-path cutover';
    END IF;
END
$$;

-- History replay (`WHERE community_id = $1 AND created_at > $2 ORDER BY
-- created_at DESC LIMIT n`) and the retention purge (`WHERE created_at < $1`).
CREATE INDEX IF NOT EXISTS idx_caption_events_recent
    ON caption_events (community_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_caption_events_created_at
    ON caption_events (created_at);

COMMENT ON COLUMN caption_events.user_ref IS
    'Tenant-tokenized UUID of the message author -- never a username or display name';
