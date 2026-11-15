"""Merge the two 0108-lineage heads into one.

`0108_bundle_precompile_requests` (executor precompiler) and `0109_platform_vault` (which chains
through `0108_revoke_cfg_communities`) landed independently on the release branch, leaving two
heads. This is an empty merge revision: it changes no schema, so a database stamped at either head
upgrades cleanly to the single new head, and `alembic upgrade head` is unambiguous again.

Revision ID: 0110_merge_precompile_vault
Revises: 0108_bundle_precompile_requests, 0109_platform_vault
Create Date: 2026-11-15
"""

from __future__ import annotations

revision = "0110_merge_precompile_vault"
down_revision = ("0108_bundle_precompile_requests", "0109_platform_vault")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge point only -- no schema change."""


def downgrade() -> None:
    """Merge point only -- downgrading just splits back into the two parent heads."""
