"""Per-user active community context: `user_active_context` (slash-command routing, hub-api only).

When a platform source (a Discord guild, a Twitch channel) is paired to several communities, the
invoking user's *active context* picks the community that processes their command (design:
``docs/superpowers/specs/2026-10-23-slash-command-architecture-design.md`` section 2).

- ``user_uuid`` -- the tokenized identity UUID the data plane already carries for the actor;
  deliberately NOT a FK to ``hub_users`` (an unlinked platform user has a token but no hub row) and
  never a raw handle or id -- no PII lives here.
- ``platform`` / ``source_id`` -- the source the context applies to (same canonical forms as the
  data-plane scope resolver); one row per ``(user_uuid, platform, source_id)``.
- ``tenant_id`` / ``community_id`` -- the chosen scope (numeric keys; the community row cascades).
- ``updated_at`` -- last switch, for audit and staleness reasoning.

Also adds ``public_id UUID`` (NOT NULL, unique, ``gen_random_uuid()`` default, backfilled) to
``tenants``, ``communities`` and ``app_catalog``: the data plane must reference entities by public
UUID across the service boundary, never by numeric PK or mutable name (critical-rules Entity
Identity & Typing). Added nullable -> backfilled -> default + NOT NULL, never a bare
``ADD COLUMN ... NOT NULL DEFAULT`` on a populated table.

Hub-api-only privileges (explicit empty rows for every other role in
`config/postgres/rbac-matrix.yaml`); the data plane reads/writes through hub-api's internal API.

Revision ID: 0084_user_active_context
Revises: 0083_eventsub_desired_state
Create Date: 2026-11-07
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from alembic import op

revision = "0084_user_active_context"
down_revision = "0083_eventsub_desired_state"
branch_labels = None
depends_on = None

_MATRIX_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "db" / "rbac_matrix.py"
_MATRIX_TABLES = frozenset({"user_active_context"})
_PUBLIC_ID_TABLES = ("tenants", "communities", "app_catalog")


def _load_matrix_module():  # type: ignore[no-untyped-def]
    """Load `scripts/db/rbac_matrix.py` by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location("waddles_rbac_matrix_0084", _MATRIX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["waddles_rbac_matrix_0084"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _add_public_id(table: str) -> None:
    """Add + backfill + constrain `public_id` on `table` (idempotent)."""
    op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS public_id UUID")
    op.execute(f"UPDATE {table} SET public_id = gen_random_uuid() WHERE public_id IS NULL")
    op.execute(f"ALTER TABLE {table} ALTER COLUMN public_id SET DEFAULT gen_random_uuid()")
    op.execute(f"ALTER TABLE {table} ALTER COLUMN public_id SET NOT NULL")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_public_id ON {table} (public_id)")


def upgrade() -> None:
    """Add public ids, create `user_active_context`, its index, and apply the RBAC grants."""
    for table in _PUBLIC_ID_TABLES:
        _add_public_id(table)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_active_context (
            id BIGSERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            user_uuid UUID NOT NULL,
            platform VARCHAR(50) NOT NULL CHECK (platform ~ '^[a-z][a-z0-9_]{0,49}$'),
            source_id VARCHAR(255) NOT NULL CHECK (source_id ~ '^[A-Za-z0-9_.:-]{1,255}$'),
            community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_user_active_context_user_source
                UNIQUE (user_uuid, platform, source_id)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE user_active_context IS "
        "'Per-user active community for a platform source (slash-command routing). user_uuid is "
        "the tokenized identity UUID (no FK, no PII). Hub-api is the only writer; the data plane "
        "goes through hub-api internal API.'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_active_context_community "
        "ON user_active_context (community_id)"
    )

    matrix_module = _load_matrix_module()
    matrix_path = os.environ.get("RBAC_MATRIX_PATH", str(matrix_module.DEFAULT_MATRIX_PATH))
    rows = matrix_module.load_matrix(matrix_path)

    for statement in matrix_module.render_revoke_public_sql(sorted(_MATRIX_TABLES)):
        op.execute(statement)
    for statement in matrix_module.render_grant_sql(rows, tables=_MATRIX_TABLES):
        op.execute(statement)

    op.execute("GRANT USAGE ON SEQUENCE user_active_context_id_seq TO hub_api;")


def downgrade() -> None:
    """Drop the table and the public ids (only this feature's API reads them)."""
    op.execute("DROP TABLE IF EXISTS user_active_context")
    for table in _PUBLIC_ID_TABLES:
        op.execute(f"DROP INDEX IF EXISTS uq_{table}_public_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS public_id")
