-- Migration 101: overlay image assets (svc-presentation-rust P6/P9) --
-- metadata for images uploaded through the authenticated PUSH-guarded
-- `POST /overlay/{community}/image/push` endpoint (P6) and resolved back
-- into a signed, scoped, expiring URL by the `Surface::image` renderer (P9,
-- `core/svc_presentation/src/images/render.rs`).
--
-- Per-service DB accounts (backend-database.md): owned by svc-presentation,
-- same shared-Postgres-separate-grant pattern migrations 073/100 already
-- established for this service.
--
-- `object_key` is this row's only pointer into SeaweedFS -- the actual
-- image bytes never round-trip through Postgres. `asset_id` (not the
-- internal `id`) is the public-facing reference a bundle/action-stage
-- adapter's `OverlayPush.image_url` field carries for a `Surface::image`
-- push (see `src/images/render.rs` module doc for why that field is
-- repurposed to hold an opaque asset reference rather than a raw
-- `http(s)://` URL for this one surface) -- a UUID, never the sequential
-- `id`, so asset references can't be enumerated.
--
-- `community_id` is `BIGINT` matching migration 100's `overlay_view_
-- credentials` (not migration 073's `INTEGER` `overlay_surfaces`/
-- `presentation_config`) -- `overlay_auth::PushCredential.community_id`
-- (the only trusted source of this value at upload time, never the request
-- body/path) is already `i64`, so this column's type matches that
-- call-site type with no cast.
--
-- `position_x`/`position_y`/`width`/`height`/`duration_ms` are captured at
-- upload time (P6's multipart fields) and read back unchanged by the P9
-- renderer -- display params live with the asset, not re-sent on every
-- push, since a pushed `image_url` is just this row's `asset_id`.
CREATE TABLE IF NOT EXISTS overlay_images (
    id              BIGSERIAL PRIMARY KEY,
    community_id    BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    asset_id        UUID NOT NULL UNIQUE,
    object_key      TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    sha256          TEXT NOT NULL,
    position_x      INTEGER,
    position_y      INTEGER,
    width           INTEGER,
    height          INTEGER,
    duration_ms     BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_overlay_images_community_id
    ON overlay_images (community_id);
