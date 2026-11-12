"""Add canvas_resolution / canvas_width / canvas_height to overlay tables.

Revision ID: 0083_overlay_canvas_dimensions
Revises: 0082_overlay_scene_components
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0083_overlay_canvas_dimensions"
down_revision = "0082_overlay_scene_components"
branch_labels = None
depends_on = None


def _add_canvas_columns(table_name: str, constraint_prefix: str) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "canvas_resolution",
            sa.String(8),
            nullable=False,
            server_default="1080p",
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "canvas_width",
            sa.Integer(),
            nullable=False,
            server_default="1920",
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "canvas_height",
            sa.Integer(),
            nullable=False,
            server_default="1080",
        ),
    )
    op.create_check_constraint(
        f"ck_{constraint_prefix}_canvas_resolution",
        table_name,
        "canvas_resolution IN ('1080p', '2k', '4k')",
    )


def _drop_canvas_columns(table_name: str, constraint_prefix: str) -> None:
    op.drop_constraint(
        f"ck_{constraint_prefix}_canvas_resolution",
        table_name,
        type_="check",
    )
    op.drop_column(table_name, "canvas_height")
    op.drop_column(table_name, "canvas_width")
    op.drop_column(table_name, "canvas_resolution")


def upgrade() -> None:
    _add_canvas_columns("community_overlay_tokens", "overlay_tokens")
    _add_canvas_columns("presentation_config", "presentation_config")
    _add_canvas_columns("overlay_surfaces", "overlay_surfaces")

    op.execute(
        """
        UPDATE community_overlay_tokens
        SET
            canvas_resolution = COALESCE(theme_config->>'resolution', '1080p'),
            canvas_width      = COALESCE((theme_config->>'width')::INTEGER,  1920),
            canvas_height     = COALESCE((theme_config->>'height')::INTEGER, 1080)
        WHERE theme_config IS NOT NULL
          AND (theme_config->>'resolution' IS NOT NULL
            OR theme_config->>'width'      IS NOT NULL
            OR theme_config->>'height'     IS NOT NULL)
        """
    )

    op.execute(
        """
        UPDATE presentation_config
        SET
            canvas_resolution = COALESCE(config->>'resolution', '1080p'),
            canvas_width      = COALESCE((config->>'width')::INTEGER,  1920),
            canvas_height     = COALESCE((config->>'height')::INTEGER, 1080)
        WHERE config IS NOT NULL
          AND (config->>'resolution' IS NOT NULL
            OR config->>'width'      IS NOT NULL
            OR config->>'height'     IS NOT NULL)
        """
    )


def downgrade() -> None:
    _drop_canvas_columns("overlay_surfaces", "overlay_surfaces")
    _drop_canvas_columns("presentation_config", "presentation_config")
    _drop_canvas_columns("community_overlay_tokens", "overlay_tokens")
