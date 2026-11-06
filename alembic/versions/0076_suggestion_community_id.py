"""Add ``ai_ticket_suggestions.community_id`` so suggestion routes can authorize by owner.

Feedback on a suggestion was keyed only by the bare suggestion id, so any authenticated caller
could write to another community's row (IDOR). The owning community is now stored at creation
(the suggest route already tenant-checks it) and backfilled from ``support_tickets`` for existing
rows. Rows whose ticket cannot be resolved stay NULL and are super-admin-only (fail closed).

Revision ID: 0076_suggestion_community_id
Revises: 0075_ai_knowledge_token_enc
Create Date: 2026-11-06
"""

from __future__ import annotations

from alembic import op

revision = "0076_suggestion_community_id"
down_revision = "0075_ai_knowledge_token_enc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable owner column, backfill from the ticket, index it."""
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('public.ai_ticket_suggestions') IS NOT NULL THEN
            ALTER TABLE ai_ticket_suggestions ADD COLUMN IF NOT EXISTS community_id INTEGER;
            IF to_regclass('public.support_tickets') IS NOT NULL THEN
              UPDATE ai_ticket_suggestions s SET community_id = t.community_id
                FROM support_tickets t
               WHERE s.ticket_id = t.id AND s.community_id IS NULL;
            END IF;
            CREATE INDEX IF NOT EXISTS idx_ai_ticket_suggestions_community_id
              ON ai_ticket_suggestions (community_id);
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    """Drop the index and column."""
    op.execute("DROP INDEX IF EXISTS idx_ai_ticket_suggestions_community_id")
    op.execute("ALTER TABLE IF EXISTS ai_ticket_suggestions DROP COLUMN IF EXISTS community_id")
