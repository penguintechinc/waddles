"""Add ``platform_connections.secret_version`` -- the credential-rotation counter.

The data-plane connector runtime restarts a live connection when its credential is rotated, but it
must learn that without ever seeing the secret. ``secret_version`` is a plain counter, starting at
1, that hub-api bumps each time an admin replaces a connection's credential (never on an OAuth
token *refresh*, which keeps the same credential identity and must not bounce live connections).
It is non-secret by construction and is served on the data-plane binding list.

Revision ID: 0074_connection_secret_version
Revises: 0073_youtube_connection_resource
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op

revision = "0074_connection_secret_version"
down_revision = "0073_youtube_connection_resource"
branch_labels = None
depends_on = None

_CHECK = "platform_connections_secret_version_check"


def upgrade() -> None:
    """Add the counter (existing rows start at 1) with a positivity CHECK."""
    op.execute(
        "ALTER TABLE platform_connections "
        "ADD COLUMN IF NOT EXISTS secret_version INTEGER NOT NULL DEFAULT 1"
    )
    op.execute(f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute(
        f"ALTER TABLE platform_connections ADD CONSTRAINT {_CHECK} CHECK (secret_version >= 1)"
    )


def downgrade() -> None:
    """Drop the counter and its CHECK."""
    op.execute(f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute("ALTER TABLE platform_connections DROP COLUMN IF EXISTS secret_version")
