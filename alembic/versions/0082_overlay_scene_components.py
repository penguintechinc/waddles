"""Create overlay_widget_registry and overlay_scene_components tables.

Revision ID: 0082_overlay_scene_components
Revises: 0081_spectrum_conn_resource
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0082_overlay_scene_components"
down_revision = "0081_spectrum_conn_resource"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "overlay_widget_registry",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "public_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "community_id",
            sa.BigInteger(),
            sa.ForeignKey("communities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bundle_app_id", sa.String(128), nullable=False),
        sa.Column("widget_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(280), nullable=False, server_default=""),
        sa.Column("category", sa.String(64), nullable=False, server_default="custom"),
        sa.Column(
            "output_kind_code",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("surface_id", sa.String(64), nullable=True),
        sa.Column("allow_standalone_output", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("default_x_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("default_y_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("default_width_px", sa.Integer(), nullable=False, server_default="400"),
        sa.Column("default_height_px", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("default_z_index", sa.Integer(), nullable=False, server_default="10"),
        sa.Column(
            "props_schema",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.UniqueConstraint("tenant_id", "community_id", "widget_id", name="uq_overlay_widget_registry_community_widget"),
        sa.CheckConstraint("output_kind_code IN (1, 2, 3, 4)", name="ck_overlay_widget_output_kind"),
        sa.CheckConstraint("default_x_px >= 0 AND default_x_px <= 3840", name="ck_overlay_widget_x"),
        sa.CheckConstraint("default_y_px >= 0 AND default_y_px <= 2160", name="ck_overlay_widget_y"),
        sa.CheckConstraint("default_width_px >= 32 AND default_width_px <= 3840", name="ck_overlay_widget_w"),
        sa.CheckConstraint("default_height_px >= 32 AND default_height_px <= 2160", name="ck_overlay_widget_h"),
        sa.CheckConstraint("default_z_index >= -100 AND default_z_index <= 1000", name="ck_overlay_widget_z"),
    )
    op.create_index(
        "idx_overlay_widget_registry_community",
        "overlay_widget_registry",
        ["tenant_id", "community_id"],
    )

    op.create_table(
        "overlay_scene_components",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "public_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "community_id",
            sa.BigInteger(),
            sa.ForeignKey("communities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bundle_app_id", sa.String(128), nullable=False),
        sa.Column("widget_id", sa.String(128), nullable=False),
        sa.Column("surface_id", sa.String(64), nullable=False, server_default="custom"),
        sa.Column(
            "output_kind_code",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("x_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("y_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("width_px", sa.Integer(), nullable=False, server_default="400"),
        sa.Column("height_px", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("z_index", sa.Integer(), nullable=False, server_default="10"),
        sa.Column(
            "status_code",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "props",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.CheckConstraint("output_kind_code IN (1, 2, 3, 4)", name="ck_scene_comp_output_kind"),
        sa.CheckConstraint("status_code IN (1, 2, 3)", name="ck_scene_comp_status"),
        sa.CheckConstraint("x_px >= 0 AND x_px <= 3840", name="ck_scene_comp_x"),
        sa.CheckConstraint("y_px >= 0 AND y_px <= 2160", name="ck_scene_comp_y"),
        sa.CheckConstraint("width_px >= 32 AND width_px <= 3840", name="ck_scene_comp_w"),
        sa.CheckConstraint("height_px >= 32 AND height_px <= 2160", name="ck_scene_comp_h"),
        sa.CheckConstraint("z_index >= -100 AND z_index <= 1000", name="ck_scene_comp_z"),
    )
    op.create_index(
        "idx_overlay_scene_components_community",
        "overlay_scene_components",
        ["tenant_id", "community_id", "z_index", "id"],
    )


def downgrade() -> None:
    op.drop_index("idx_overlay_scene_components_community", table_name="overlay_scene_components")
    op.drop_table("overlay_scene_components")
    op.drop_index("idx_overlay_widget_registry_community", table_name="overlay_widget_registry")
    op.drop_table("overlay_widget_registry")

