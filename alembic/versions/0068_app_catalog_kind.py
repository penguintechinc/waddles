"""Add nullable denormalized `app_catalog.kind` column.

Listing convenience only -- the manifest remains the source of truth.
Nullable for back-compat: pre-existing rows carry no kind.

Revision ID: 0068_app_catalog_kind
Revises: 0067_merge_heads_20261028
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068_app_catalog_kind"
down_revision = "0067_merge_heads_20261028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add `app_catalog.kind` (nullable VARCHAR(20))."""
    op.add_column("app_catalog", sa.Column("kind", sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Drop `app_catalog.kind`."""
    op.drop_column("app_catalog", "kind")
