"""DSAR public requests: pending content draft tied to the latest verification token.

A resubmit for an already-pending public request used to overwrite the committed `message_enc` /
`regime`, letting an unauthenticated third party rewrite what the mailbox owner had written. The
resubmitted content now waits in a DRAFT (`pending_message_enc`, `pending_regime`, bound to the token it was created under by
`pending_token_hash`) and is copied into the committed columns only when exactly that token is
consumed (the mailbox owner clicked it); a bare resend clears the draft.
`pending_regime IS NOT NULL` marks that a draft exists (its message may legitimately be NULL).

Both columns are nullable and unindexed; the encrypted draft is blanked with the other contact data.

Revision ID: 0128_dsar_pending_draft
Revises: 0127_tenant_license_key
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision = "0128_dsar_pending_draft"
down_revision = "0127_tenant_license_key"
branch_labels = None
depends_on = None

_UPGRADE = (
    "ALTER TABLE dsar_requests ADD COLUMN IF NOT EXISTS pending_message_enc TEXT",
    "ALTER TABLE dsar_requests ADD COLUMN IF NOT EXISTS pending_regime SMALLINT",
    "ALTER TABLE dsar_requests ADD COLUMN IF NOT EXISTS pending_token_hash TEXT",
    (
        "ALTER TABLE dsar_requests ADD CONSTRAINT ck_dsar_requests_pending_regime "
        "CHECK (pending_regime IS NULL OR pending_regime BETWEEN 1 AND 3)"
    ),
)

_DOWNGRADE = (
    "ALTER TABLE dsar_requests DROP CONSTRAINT IF EXISTS ck_dsar_requests_pending_regime",
    "ALTER TABLE dsar_requests DROP COLUMN IF EXISTS pending_token_hash",
    "ALTER TABLE dsar_requests DROP COLUMN IF EXISTS pending_regime",
    "ALTER TABLE dsar_requests DROP COLUMN IF EXISTS pending_message_enc",
)


def upgrade() -> None:
    """Add the draft columns and their regime range check."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Drop the draft columns (an unconsumed draft is discarded; committed content is untouched)."""
    for statement in _DOWNGRADE:
        op.execute(statement)
