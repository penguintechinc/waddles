"""Overlay builder: widget registry, composite-scene components, canvas resolution.

Adds two tenant- and community-scoped tables and one column:

  overlay_widget_registry     widgets a presentation bundle registered for a community
  overlay_scene_components    widget instances placed on a community's composite scene
  presentation_config.canvas_resolution_code
                              1 = 1080p (1920x1080), 2 = 2k (2560x1440), 3 = 4k (3840x2160)

`presentation_config` is the row svc-presentation already reads per community, so the canvas the
Designer sets reaches the data plane without a hub-api hop. Width/height are derived from the
code (never stored twice). Every kind/status column is an int code; `public_id` is the only
external reference (the numeric `id` never leaves the database).

  output_kind_code  1 text, 2 image, 3 sanitized_html, 4 surface
  status_code       1 visible, 2 hidden

Chained after 0110_merge_precompile_vault (#1354); 0111/0112 are reserved by other in-flight
work, whoever merges later re-points down_revision so the chain stays single-headed.

Revision ID: 0113_overlay_scene_components
Revises: 0110_merge_precompile_vault
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0113_overlay_scene_components"
down_revision = "0110_merge_precompile_vault"
branch_labels = None
depends_on = None

_UUID_DEFAULT = sa.text("gen_random_uuid()")
_EMPTY_JSON = sa.text("'{}'::jsonb")


def _common_columns() -> list[sa.Column]:
    """Identity, tenant/community scope and audit timestamps shared by both tables."""
    return [
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "public_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=_UUID_DEFAULT,
        ),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "community_id",
            sa.BigInteger(),
            sa.ForeignKey("communities.id", ondelete="CASCADE"),
            nullable=False,
        ),
    ]


def _timestamps() -> list[sa.Column]:
    """created_at / updated_at pair."""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def upgrade() -> None:
    """Create the two overlay tables and the canvas column."""
    op.create_table(
        "overlay_widget_registry",
        *_common_columns(),
        sa.Column("bundle_app_id", sa.String(128), nullable=False),
        sa.Column("widget_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(280), nullable=False, server_default=""),
        sa.Column("category", sa.String(64), nullable=False, server_default="custom"),
        sa.Column(
            "output_kind_code", sa.SmallInteger(), nullable=False, server_default="1"
        ),
        sa.Column("surface_id", sa.String(64), nullable=True),
        sa.Column(
            "allow_standalone_output",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("default_x_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("default_y_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column(
            "default_width_px", sa.Integer(), nullable=False, server_default="400"
        ),
        sa.Column(
            "default_height_px", sa.Integer(), nullable=False, server_default="200"
        ),
        sa.Column("default_z_index", sa.Integer(), nullable=False, server_default="10"),
        sa.Column(
            "props_schema",
            postgresql.JSONB(),
            nullable=False,
            server_default=_EMPTY_JSON,
        ),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "community_id",
            "widget_id",
            name="uq_overlay_widget_registry_widget",
        ),
        sa.CheckConstraint(
            "output_kind_code IN (1, 2, 3, 4)", name="ck_overlay_widget_kind"
        ),
        sa.CheckConstraint(
            "default_x_px BETWEEN 0 AND 3840", name="ck_overlay_widget_x"
        ),
        sa.CheckConstraint(
            "default_y_px BETWEEN 0 AND 2160", name="ck_overlay_widget_y"
        ),
        sa.CheckConstraint(
            "default_width_px BETWEEN 32 AND 3840", name="ck_overlay_widget_w"
        ),
        sa.CheckConstraint(
            "default_height_px BETWEEN 32 AND 2160", name="ck_overlay_widget_h"
        ),
        sa.CheckConstraint(
            "default_z_index BETWEEN -1000 AND 1000", name="ck_overlay_widget_z"
        ),
    )
    op.create_index(
        "idx_overlay_widget_registry_scope",
        "overlay_widget_registry",
        ["tenant_id", "community_id"],
    )

    op.create_table(
        "overlay_scene_components",
        *_common_columns(),
        sa.Column("bundle_app_id", sa.String(128), nullable=False),
        sa.Column("widget_id", sa.String(128), nullable=False),
        sa.Column("surface_id", sa.String(64), nullable=False, server_default="custom"),
        sa.Column(
            "output_kind_code", sa.SmallInteger(), nullable=False, server_default="1"
        ),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("x_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("y_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("width_px", sa.Integer(), nullable=False, server_default="400"),
        sa.Column("height_px", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("z_index", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("status_code", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column(
            "props", postgresql.JSONB(), nullable=False, server_default=_EMPTY_JSON
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "output_kind_code IN (1, 2, 3, 4)", name="ck_scene_comp_kind"
        ),
        sa.CheckConstraint("status_code IN (1, 2)", name="ck_scene_comp_status"),
        sa.CheckConstraint("x_px BETWEEN 0 AND 3840", name="ck_scene_comp_x"),
        sa.CheckConstraint("y_px BETWEEN 0 AND 2160", name="ck_scene_comp_y"),
        sa.CheckConstraint("width_px BETWEEN 32 AND 3840", name="ck_scene_comp_w"),
        sa.CheckConstraint("height_px BETWEEN 32 AND 2160", name="ck_scene_comp_h"),
        sa.CheckConstraint("z_index BETWEEN -1000 AND 1000", name="ck_scene_comp_z"),
    )
    op.create_index(
        "idx_overlay_scene_components_scope",
        "overlay_scene_components",
        ["tenant_id", "community_id", "z_index", "id"],
    )

    # `presentation_config` is normally created by legacy SQL migration 073, but that replay does
    # not run on every chain (fresh databases), so create-if-absent with the identical shape.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS presentation_config (
            id                     SERIAL PRIMARY KEY,
            community_id           INTEGER NOT NULL UNIQUE
                                   REFERENCES communities(id) ON DELETE CASCADE,
            theme                  TEXT DEFAULT 'default',
            primary_color          TEXT,
            secondary_color        TEXT,
            font_family            TEXT,
            music_enabled          BOOLEAN DEFAULT TRUE,
            crawler_speed_seconds  INTEGER DEFAULT 30,
            config                 JSONB DEFAULT '{}',
            created_at             TIMESTAMPTZ DEFAULT NOW(),
            updated_at             TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    op.execute(
        "ALTER TABLE presentation_config "
        "ADD COLUMN IF NOT EXISTS canvas_resolution_code SMALLINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE presentation_config DROP CONSTRAINT IF EXISTS ck_presentation_config_canvas"
    )
    op.create_check_constraint(
        "ck_presentation_config_canvas",
        "presentation_config",
        "canvas_resolution_code IN (1, 2, 3)",
    )


def downgrade() -> None:
    """Drop the canvas column and both overlay tables."""
    op.execute(
        "ALTER TABLE presentation_config DROP CONSTRAINT IF EXISTS ck_presentation_config_canvas"
    )
    op.execute(
        "ALTER TABLE presentation_config DROP COLUMN IF EXISTS canvas_resolution_code"
    )
    op.drop_index(
        "idx_overlay_scene_components_scope", table_name="overlay_scene_components"
    )
    op.drop_table("overlay_scene_components")
    op.drop_index(
        "idx_overlay_widget_registry_scope", table_name="overlay_widget_registry"
    )
    op.drop_table("overlay_widget_registry")
