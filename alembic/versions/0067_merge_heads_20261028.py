"""Merge divergent heads 0062_encrypt_vendor_payment, 0062_live_activity_actor_id, 0066_one_time_secrets.

Schema-neutral merge so `alembic upgrade head` resolves to a single head.

Revision ID: 0067_merge_heads_20261028
Revises: 0062_encrypt_vendor_payment, 0062_live_activity_actor_id, 0066_one_time_secrets
Create Date: 2026-10-28
"""

from __future__ import annotations

revision = "0067_merge_heads_20261028"
down_revision = (
    "0062_encrypt_vendor_payment",
    "0062_live_activity_actor_id",
    "0066_one_time_secrets",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: merge point only."""


def downgrade() -> None:
    """No-op: splits back into the three prior heads."""
