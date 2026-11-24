"""platform_connections.ownership_verified: bind-time proof that the installer owns the channel.

Security finding (High, release @1e4d2ee34): binding a Twitch/Kick/YouTube channel never proved
ownership, and uniqueness was only per tenant, so another tenant could claim a victim's channel and
svc-ingest's dedup then dropped BOTH claimants. hub-api now proves ownership at bind time and sets
``ownership_verified`` (legacy rows default FALSE and keep working until re-verified). A channel may
be bound to many communities across tenants (N:M, user decision 2026-11-12), so there is NO global
unique constraint: uniqueness stays per (tenant, platform, resource) + per-community grant.

RECHAIN NEEDED: written against head 0101_svc_ingest_display_names while PRs #1241/#1246/#1249 hold
0100-0102 open; whichever merges first, re-point ``down_revision`` at the then-current head.

Revision ID: 0103_conn_ownership_verified
Revises: 0101_svc_ingest_display_names
Create Date: 2026-11-12
"""

from __future__ import annotations

from alembic import op

revision = "0103_conn_ownership_verified"
down_revision = "0101_svc_ingest_display_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the flag (default FALSE: legacy rows are unverified)."""
    op.execute(
        "ALTER TABLE platform_connections "
        "ADD COLUMN IF NOT EXISTS ownership_verified BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    """Drop the column (back to 0101_svc_ingest_display_names)."""
    op.execute("ALTER TABLE platform_connections DROP COLUMN IF EXISTS ownership_verified")
