"""Merge divergent heads 0108_bundle_precompile_requests and 0109_platform_vault.

Both revisions landed independently off 0107 (via 0108_revoke_cfg_communities for the vault), leaving
two heads so `alembic upgrade head` failed. Schema-neutral merge point so the graph has exactly one head.

Revision ID: 0110_merge_precompile_vault
Revises: 0108_bundle_precompile_requests, 0109_platform_vault
Create Date: 2026-11-14
"""

from __future__ import annotations

revision = "0110_merge_precompile_vault"
down_revision = (
    "0108_bundle_precompile_requests",
    "0109_platform_vault",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: merge point only."""


def downgrade() -> None:
    """No-op: splits back into the two prior heads."""
