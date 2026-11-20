"""Idempotency key on the token ledger so a retried debit cannot double-charge.

Adds `token_transactions.idempotency_key` (nullable; stored as `<community_id>:<product_id>:<key>`)
with a UNIQUE index, plus `actor_user_id` (the member the debit was for) so a reused key from a
different actor is refused rather than replayed. NULL for un-keyed rows (the index ignores them), so existing rows and older
hub-api pods are unaffected. Downgrade drops the index and column.

Revision ID: 0135_token_debit_idempotency
Revises: 0134_shoutout_history_drift
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision = "0135_token_debit_idempotency"
down_revision = "0134_shoutout_history_drift"
branch_labels = None
depends_on = None

# Guarded on the table existing: migration 076 creates it on every real deployment, but minimal
# test databases (and a fresh DB mid-replay) may not have it yet -- then this is a no-op.
UPGRADE_SQL = (
    """
DO $$
BEGIN
    IF to_regclass('token_transactions') IS NOT NULL THEN
        ALTER TABLE token_transactions ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255);
        ALTER TABLE token_transactions ADD COLUMN IF NOT EXISTS actor_user_id INTEGER;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_token_transactions_idempotency_key
            ON token_transactions (idempotency_key) WHERE idempotency_key IS NOT NULL;
    END IF;
END
$$
""",
)


def upgrade() -> None:
    """Add the nullable idempotency column and its partial unique index (idempotent)."""
    for stmt in UPGRADE_SQL:
        op.execute(stmt)


def downgrade() -> None:
    """Drop the unique index, then the column."""
    op.execute("DROP INDEX IF EXISTS uq_token_transactions_idempotency_key")
    op.execute("ALTER TABLE IF EXISTS token_transactions DROP COLUMN IF EXISTS idempotency_key")
    op.execute("ALTER TABLE IF EXISTS token_transactions DROP COLUMN IF EXISTS actor_user_id")
