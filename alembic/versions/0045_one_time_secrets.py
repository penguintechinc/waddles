"""`one_time_secrets` -- encrypted, single-pull, expiring secret messages (feature #684).

Backs `!secret <user> <msg>`: the bundle stores the message here, DMs the
target a link, and the target pulls it once through hub-api. Security
shape (security.md):

- `token_hash` is SHA-256 of the random link token -- the raw token is
  shown to the creator exactly once and never stored, so a DB read cannot
  mint a working link.
- `ciphertext`/`iv` hold the AES-256-GCM-encrypted body (AAD = row id);
  there is no plaintext column.
- `target_user_uuid` is `hub_users.uuid` (migration 0043) -- UUID only,
  never a username or other PII.
- `pulled_at` is the atomic-claim marker: hub-api sets it with a single
  conditional UPDATE, and the row is hard-deleted right after the read.

Revision ID: 0045_one_time_secrets
Revises: 0044_connector_pii_reader_role
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0045_one_time_secrets"
down_revision = "0044_connector_pii_reader_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the `one_time_secrets` table and its lookup/expiry indexes."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS one_time_secrets (
            id UUID PRIMARY KEY,
            token_hash CHAR(64) NOT NULL UNIQUE,
            tenant_id INTEGER NOT NULL,
            community_id INTEGER NOT NULL,
            target_user_uuid UUID NOT NULL,
            ciphertext BYTEA NOT NULL,
            iv BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            pulled_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_one_time_secrets_expires_at "
        "ON one_time_secrets (expires_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_one_time_secrets_target "
        "ON one_time_secrets (target_user_uuid)"
    )


def downgrade() -> None:
    """Drop the table (indexes go with it)."""
    op.execute("DROP TABLE IF EXISTS one_time_secrets")
