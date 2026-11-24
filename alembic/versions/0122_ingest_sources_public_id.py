"""`ingest_sources.public_id` -- the UUID that crosses the API boundary.

`ingest_sources.id` is a BIGSERIAL surrogate key for internal joins only (workstreams,
app_source_bindings). Exposing it in URLs/bodies made sources enumerable (IDOR), so the
v1 `ingest-sources` API now identifies a source by `public_id`. Same add/backfill/constrain
idiom as migration 0084's `_add_public_id`, safe to re-run.

Communities already carry `public_id` (migration 0084), so no change there.

Revision ID: 0122_ingest_sources_public_id
Revises: 0121_dsar_requests
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op

revision = "0122_ingest_sources_public_id"
down_revision = "0121_dsar_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add `public_id` atomically (distinct non-null UUID per row), then constrain it.

    `ADD COLUMN ... NOT NULL DEFAULT gen_random_uuid()` evaluates the volatile default once per
    existing row (a table rewrite under ACCESS EXCLUSIVE, short for this small table) and is
    atomic, so a concurrent INSERT from a still-running old pod can never land a NULL between
    "add" and "set default" (the previous add-nullable/backfill/default/NOT NULL order could, and
    SET NOT NULL then failed). The follow-up statements only repair a half-applied earlier run.
    """
    op.execute(
        "ALTER TABLE ingest_sources "
        "ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()"
    )
    op.execute(
        "UPDATE ingest_sources SET public_id = gen_random_uuid() WHERE public_id IS NULL"
    )
    op.execute(
        "ALTER TABLE ingest_sources ALTER COLUMN public_id SET DEFAULT gen_random_uuid()"
    )
    op.execute("ALTER TABLE ingest_sources ALTER COLUMN public_id SET NOT NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ingest_sources_public_id "
        "ON ingest_sources (public_id)"
    )


def downgrade() -> None:
    """Drop the unique index and the column."""
    op.execute("DROP INDEX IF EXISTS uq_ingest_sources_public_id")
    op.execute("ALTER TABLE ingest_sources DROP COLUMN IF EXISTS public_id")
