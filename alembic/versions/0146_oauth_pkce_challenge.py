"""Add PKCE `code_challenge` to `hub_oauth_states` and `hub_oauth_exchange_codes`.

The desktop (`waddles://oauth/callback`) OAuth login has no browser-binding cookie, so the
exchange code it receives could be intercepted and redeemed by another local app. RFC 7636
PKCE (S256) closes that: the client's challenge is stored on the state row, carried onto the
exchange-code row, and `POST /api/v1/auth/exchange` requires the matching `code_verifier`.

- `code_challenge VARCHAR(64)` on both tables -- nullable; browser flows never set it.

Idempotent (`IF NOT EXISTS`) and fresh-replay safe: a missing table is a no-op.

Revision ID: 0146_oauth_pkce_challenge
Revises: 0145_radio_station_default
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0146_oauth_pkce_challenge"
down_revision = "0145_radio_station_default"
branch_labels = None
depends_on = None

_TABLES = ("hub_oauth_states", "hub_oauth_exchange_codes")


def upgrade() -> None:
    """Add the nullable PKCE challenge column to both OAuth handoff tables."""
    for table in _TABLES:
        op.execute(
            f"""
            DO $$ BEGIN
              IF to_regclass('public.{table}') IS NOT NULL THEN
                ALTER TABLE {table} ADD COLUMN IF NOT EXISTS code_challenge VARCHAR(64);
              END IF;
            END $$;
            """
        )


def downgrade() -> None:
    """Drop the PKCE challenge column from both tables."""
    for table in _TABLES:
        op.execute(
            f"""
            DO $$ BEGIN
              IF to_regclass('public.{table}') IS NOT NULL THEN
                ALTER TABLE {table} DROP COLUMN IF EXISTS code_challenge;
              END IF;
            END $$;
            """
        )
