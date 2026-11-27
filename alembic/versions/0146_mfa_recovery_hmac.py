"""MFA recovery codes: peppered HMAC-SHA256 instead of plain SHA-256.

`hub_user_mfa_recovery_codes.code_hash` becomes HMAC-SHA256 of the normalized code under a
pepper derived from the ``system`` tenant's envelope data key (`services/envelope`, platform
KEK baseline / BYOK). The new NOT NULL `key_version` column records which DEK version produced
the pepper, so a DEK rotation keeps old codes verifiable.

Migration path for existing rows: plain SHA-256 hashes cannot be re-keyed without the
plaintext codes (shown once, never stored), and they only exist on dev databases (0145 has not
shipped). They are deleted here. Affected users keep their TOTP / email factors; the security
page shows 0 recovery codes left and they regenerate a fresh set (re-auth + a valid code).

Revision ID: 0146_mfa_recovery_hmac
Revises: 0145_hub_user_mfa
Create Date: 2026-11-27
"""

from __future__ import annotations

from alembic import op

revision = "0146_mfa_recovery_hmac"
down_revision = "0145_hub_user_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop legacy unkeyed hashes, then add the NOT NULL `key_version` column."""
    op.execute("DELETE FROM hub_user_mfa_recovery_codes")
    op.execute(
        "ALTER TABLE hub_user_mfa_recovery_codes "
        "ADD COLUMN IF NOT EXISTS key_version INTEGER NOT NULL"
    )


def downgrade() -> None:
    """Remove the column; peppered hashes are useless to the old code path, so drop them too."""
    op.execute("DELETE FROM hub_user_mfa_recovery_codes")
    op.execute("ALTER TABLE hub_user_mfa_recovery_codes DROP COLUMN IF EXISTS key_version")
