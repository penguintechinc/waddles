"""Email verification tokens are stored as SHA-256 digests only: drop legacy raw tokens.

``hub_api/services/email_verification.py`` now stores ``hub_users.email_verification_token`` as
a 64-char SHA-256 hex digest and looks tokens up by digest. Rows written by the previous code
hold the RAW token (``secrets.token_urlsafe(32)``, 43 chars) -- a usable credential at rest, and
one that can no longer match. This revision clears them (and their expiry); affected users
request a fresh link via ``POST /api/v1/auth/resend-verification``. No schema change.

Idempotent: a digest is exactly 64 chars, so a re-run touches nothing. Downgrade is a no-op
(the raw tokens are gone by design and are never recreated).

Revision ID: 0145_email_verif_token_digest
Revises: 0144_ingest_owner_public_ids
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0145_email_verif_token_digest"
down_revision = "0144_ingest_owner_public_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Null every non-digest verification token (and its expiry) on hub_users."""
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("hub_users"):
        return  # partial/ordered replays without the identity table: nothing to clear
    columns = {c["name"] for c in inspector.get_columns("hub_users")}
    if not {"email_verification_token", "email_verification_expires"} <= columns:
        return  # schema variants without the verification columns hold no raw tokens
    op.execute(
        "UPDATE hub_users SET email_verification_token = NULL, email_verification_expires = NULL "
        "WHERE email_verification_token IS NOT NULL AND LENGTH(email_verification_token) <> 64"
    )


def downgrade() -> None:
    """No-op: cleared raw tokens are intentionally not restored."""
