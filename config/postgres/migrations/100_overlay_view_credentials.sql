-- Migration 100: unified overlay-auth contract (C4) -- the per-community
-- VIEW credential table that `core/overlay_auth` (new standalone Rust
-- crate) validates browser-source GET requests against. Backs the future
-- svc-presentation-rust's `/overlay/<community>/<surface>` guard (P4/P5);
-- today's Python svc-presentation serves those routes with NO auth at all
-- (`services/surfaces.py::is_valid_community()` is a syntax check only) --
-- this table + the crate close that gap once P4/P5 wire the guard in.
--
-- Per-service DB accounts (backend-database.md): owned by svc-presentation,
-- same shared-Postgres-separate-grant pattern migration 073's
-- `overlay_surfaces`/`presentation_config` already established for this
-- service -- NOT appended to hub-api's `community_overlay_tokens` (that
-- table stays hub-api's own, used by its admin CRUD/rotate endpoints,
-- `hub_api/blueprints/v1/overlay.py`).
--
-- Only a SHA-256 hash of the opaque view token is ever stored -- never the
-- plaintext credential (security.md Encryption, defense-in-depth: a DB
-- read/backup leak no longer hands out live overlay credentials the way
-- `community_overlay_tokens.overlay_key` plaintext storage does today).
-- `previous_key_hash`/`rotated_at` reproduce the legacy 5-minute rotation
-- grace window (`core/browser_source_core_module/services/overlay_service
-- .py::KEY_GRACE_PERIOD_MINUTES`) so rotating a key never hard-cuts an
-- already-live OBS browser-source session.
--
-- Legacy migration path: the backfill below hashes every existing
-- `community_overlay_tokens` row's plaintext `overlay_key`/`previous_key`
-- into this table. Existing OBS browser-source URLs (which embed the raw
-- `overlay_key` as the `?key=` value, unchanged) keep validating
-- unmodified once P4/P5 switch the GET route to this table -- zero
-- customer action, zero downtime. hub-api's own admin rotate endpoint
-- (`services/overlay_service.py::rotate_overlay_key`) still writes the
-- legacy table only as of this migration; P4/P5 tracks the follow-up to
-- make hub-api write-through to `overlay_view_credentials` (hashed) on
-- every create/rotate so the two tables never drift after today's
-- one-time backfill -- flagged for security review, see PR description.

CREATE TABLE IF NOT EXISTS overlay_view_credentials (
    id                 BIGSERIAL PRIMARY KEY,
    community_id       BIGINT NOT NULL UNIQUE REFERENCES communities(id) ON DELETE CASCADE,
    key_hash           TEXT NOT NULL UNIQUE,
    previous_key_hash  TEXT,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    rotated_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_overlay_view_credentials_key_hash
    ON overlay_view_credentials (key_hash);

CREATE INDEX IF NOT EXISTS idx_overlay_view_credentials_previous_key_hash
    ON overlay_view_credentials (previous_key_hash)
    WHERE previous_key_hash IS NOT NULL;

-- One-time backfill: hash legacy plaintext keys forward. Built-in
-- `sha256()` (PostgreSQL 14+, no pgcrypto extension needed) -- matches
-- `rules/backend-database.md` PostgreSQL 17.x baseline. Re-running this
-- migration is a no-op (`ON CONFLICT ... DO NOTHING`) since
-- `community_id` is UNIQUE here exactly as it is on the legacy table.
INSERT INTO overlay_view_credentials
    (community_id, key_hash, previous_key_hash, is_active, rotated_at, created_at, updated_at)
SELECT
    community_id,
    encode(sha256(overlay_key::bytea), 'hex'),
    CASE WHEN previous_key IS NOT NULL THEN encode(sha256(previous_key::bytea), 'hex') END,
    is_active,
    rotated_at,
    created_at,
    updated_at
FROM community_overlay_tokens
ON CONFLICT (community_id) DO NOTHING;
