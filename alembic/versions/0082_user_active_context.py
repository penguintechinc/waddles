"""Per-user active community context + the community UUID it is addressed by.

Slash interactions are guild-wide, so when a platform source (a Discord guild) is paired to several
communities the invoking USER's active context picks the processing community
(`docs/superpowers/specs/2026-10-23-slash-command-architecture-design.md` section 2). svc-ingest
asks hub-api (`/api/v1/internal/dataplane/active-context`) and addresses communities by an
immutable UUID, never a name or a sequential id, so this adds:

- ``communities.public_id`` -- ``UUID NOT NULL DEFAULT gen_random_uuid()``, unique. Existing rows are
  backfilled by the volatile default (each row gets its own value).
- ``user_active_contexts`` -- ``(platform, source_id, user_uuid) -> community_id``. ``user_uuid`` is
  the tokenized user UUID (``hub_users.uuid``); hub-api never stores a platform user id here. The
  erasure registry deletes these rows with the subject.

Revision ID: 0082_user_active_context
Revises: 0081_spectrum_conn_resource
Create Date: 2026-11-08
"""

from __future__ import annotations

from alembic import op

revision = "0082_user_active_context"
down_revision = "0081_spectrum_conn_resource"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add ``communities.public_id`` and create ``user_active_contexts``."""
    op.execute(
        "ALTER TABLE communities "
        "ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_communities_public_id ON communities (public_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_active_contexts (
            id BIGSERIAL PRIMARY KEY,
            platform VARCHAR(50) NOT NULL,
            source_id VARCHAR(255) NOT NULL,
            user_uuid UUID NOT NULL,
            community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_user_active_contexts_actor UNIQUE (platform, source_id, user_uuid)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_active_contexts_user ON user_active_contexts (user_uuid)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_active_contexts_community "
        "ON user_active_contexts (community_id)"
    )


def downgrade() -> None:
    """Drop the table and the column."""
    op.execute("DROP TABLE IF EXISTS user_active_contexts")
    op.execute("DROP INDEX IF EXISTS uq_communities_public_id")
    op.execute("ALTER TABLE communities DROP COLUMN IF EXISTS public_id")
