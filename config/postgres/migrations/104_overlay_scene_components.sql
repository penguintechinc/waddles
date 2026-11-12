-- Migration 104: overlay builder scene components and widget registry --
-- Stores community-placed widget instances on the composite overlay canvas (1920x1080)
-- and presentation bundle widget registrations.
--
-- Per-service DB accounts (backend-database.md): tenant-scoped and community-scoped.
-- Primary key is BIGINT internal surrogate PK; external API boundaries use public_id UUID.

CREATE TABLE IF NOT EXISTS overlay_widget_registry (
    id                      BIGSERIAL PRIMARY KEY,
    public_id               UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    tenant_id               BIGINT NOT NULL,
    community_id            BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    bundle_app_id           VARCHAR(128) NOT NULL,
    widget_id               VARCHAR(128) NOT NULL,
    display_name            VARCHAR(120) NOT NULL,
    description             VARCHAR(280) NOT NULL DEFAULT '',
    category                VARCHAR(64)  NOT NULL DEFAULT 'custom',
    output_kind_code        SMALLINT NOT NULL DEFAULT 1 CHECK (output_kind_code IN (1, 2, 3, 4)),
    surface_id              VARCHAR(64),
    allow_standalone_output BOOLEAN NOT NULL DEFAULT TRUE,
    default_x_px            INTEGER NOT NULL DEFAULT 48 CHECK (default_x_px >= 0 AND default_x_px <= 3840),
    default_y_px            INTEGER NOT NULL DEFAULT 48 CHECK (default_y_px >= 0 AND default_y_px <= 2160),
    default_width_px        INTEGER NOT NULL DEFAULT 400 CHECK (default_width_px >= 32 AND default_width_px <= 3840),
    default_height_px       INTEGER NOT NULL DEFAULT 200 CHECK (default_height_px >= 32 AND default_height_px <= 2160),
    default_z_index         INTEGER NOT NULL DEFAULT 10 CHECK (default_z_index >= -100 AND default_z_index <= 1000),
    props_schema            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, community_id, widget_id)
);

CREATE INDEX IF NOT EXISTS idx_overlay_widget_registry_community
    ON overlay_widget_registry (tenant_id, community_id);

CREATE TABLE IF NOT EXISTS overlay_scene_components (
    id                      BIGSERIAL PRIMARY KEY,
    public_id               UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    tenant_id               BIGINT NOT NULL,
    community_id            BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    bundle_app_id           VARCHAR(128) NOT NULL,
    widget_id               VARCHAR(128) NOT NULL,
    surface_id              VARCHAR(64)  NOT NULL DEFAULT 'custom',
    output_kind_code        SMALLINT NOT NULL DEFAULT 1 CHECK (output_kind_code IN (1, 2, 3, 4)),
    display_name            VARCHAR(120) NOT NULL,
    x_px                    INTEGER NOT NULL DEFAULT 48 CHECK (x_px >= 0 AND x_px <= 3840),
    y_px                    INTEGER NOT NULL DEFAULT 48 CHECK (y_px >= 0 AND y_px <= 2160),
    width_px                INTEGER NOT NULL DEFAULT 400 CHECK (width_px >= 32 AND width_px <= 3840),
    height_px               INTEGER NOT NULL DEFAULT 200 CHECK (height_px >= 32 AND height_px <= 2160),
    z_index                 INTEGER NOT NULL DEFAULT 10 CHECK (z_index >= -100 AND z_index <= 1000),
    status_code             SMALLINT NOT NULL DEFAULT 1 CHECK (status_code IN (1, 2, 3)),
    props                   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_overlay_scene_components_community
    ON overlay_scene_components (tenant_id, community_id, z_index, id);

