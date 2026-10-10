"""Provider/connection framework: per-connection metadata + expiry, per-community `enabled`.

Foundation of the standard provider/connection framework (Step 1 of the roadmap in
`docs/CONNECTION_MODEL.local.md`). It builds on -- it does NOT replace -- the three-layer model
migration 0035 landed, so the existing `platform_connections` (layer 2: the install + its
AES-256-GCM encrypted tokens) and `community_connection_access` (layer 3: which community may use
a connection, no tokens) stay the only places a connection lives. This migration only adds the
columns the generic connection API needs:

``platform_connections`` (layer 2 -- facts about the *link*, shared by every community using it)

- ``resource_metadata JSONB NOT NULL DEFAULT '{}'`` -- adapter-validated, provider-specific
  facts (for Twitch: the bot account the token belongs to, the resolved channel, granted scopes).
  Never a secret: tokens stay in ``access_token`` / ``refresh_token``. Named
  ``resource_metadata`` (not ``metadata``) because ``metadata`` is reserved on SQLAlchemy
  declarative models and shadows ``Table.metadata`` on reflected tables.
- ``expires_at TIMESTAMPTZ`` -- when ``access_token`` stops working (NULL = does not expire).
  Drives refresh-before-use; Twitch user tokens live ~4 hours.
- ``status_reason VARCHAR(40)`` -- why ``status`` left ``active`` (``refresh_rejected``,
  ``token_expired``, ...), a fixed machine code so the UI can say "reconnect" without ever
  echoing provider text.

``community_connection_access`` (layer 3 -- per-community switches)

- ``enabled BOOLEAN NOT NULL DEFAULT TRUE`` -- each community independently turns its use of a
  shared connection on/off without touching the connection or any other community. DEFAULT TRUE
  keeps every pre-existing grant behaving exactly as before.

The ``resource_type`` allow-list CHECK from 0035 is deliberately left alone: a new provider that
introduces a new resource kind extends that CHECK in its own one-line migration (a deliberate,
reviewable DB-level allow-list), and leaving it untouched avoids colliding with other in-flight
migrations that also extend it.

Purely additive and online-safe: ``ADD COLUMN ... DEFAULT`` with a constant default is a
metadata-only change on PostgreSQL 11+, no rewrite. Table privileges are table-level in
``config/postgres/rbac-matrix.yaml`` (``hub_api`` read/write; every data-plane role
``privileges: []``), so the new columns inherit them with no matrix change -- the data plane
still cannot read a token column and reaches connections only through hub-api.

**Numbering note:** parallel migrations are landing on ``release/v3.0.X``; this is chained off
``0053_audit_events_hash_chain`` (the head when written). Re-point ``down_revision`` at the
then-current head if another migration merges first
(``bash scripts/ci/check-alembic-single-head.sh``).

Revision ID: 0056_provider_connections
Revises: 0053_audit_events_hash_chain
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op

revision = "0056_provider_connections"
down_revision = "0053_audit_events_hash_chain"
branch_labels = None
depends_on = None

_STATUS_REASON_CHECK = "platform_connections_status_reason_check"
_PLATFORM_STATUS_INDEX = "idx_platform_connections_platform_status"


def upgrade() -> None:
    """Add the framework columns, the status-reason CHECK, and the data-plane lookup index."""
    op.execute(
        """
        ALTER TABLE platform_connections
            ADD COLUMN IF NOT EXISTS resource_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS status_reason VARCHAR(40)
        """
    )
    op.execute(
        f"""
        ALTER TABLE platform_connections
            ADD CONSTRAINT {_STATUS_REASON_CHECK}
            CHECK (status_reason IS NULL OR status_reason ~ '^[a-z][a-z0-9_]*$')
        """
    )
    op.execute(
        "ALTER TABLE community_connection_access "
        "ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_PLATFORM_STATUS_INDEX} "
        "ON platform_connections (platform, status)"
    )
    op.execute(
        "COMMENT ON COLUMN platform_connections.resource_metadata IS "
        "'Adapter-validated, provider-specific facts about the link (bot account, resolved "
        "channel, scopes). Never a secret -- tokens live only in access_token/refresh_token.'"
    )
    op.execute(
        "COMMENT ON COLUMN platform_connections.expires_at IS "
        "'When access_token stops working (NULL = does not expire); drives refresh-before-use.'"
    )
    op.execute(
        "COMMENT ON COLUMN platform_connections.status_reason IS "
        "'Fixed machine code for why status left active (refresh_rejected, token_expired, ...). "
        "Never provider text.'"
    )
    op.execute(
        "COMMENT ON COLUMN community_connection_access.enabled IS "
        "'Per-community on/off switch for using the connection; independent of the "
        "connection itself and of every other community.'"
    )


def downgrade() -> None:
    """Drop everything `upgrade()` added (column data is discarded -- tokens are untouched)."""
    op.execute(f"DROP INDEX IF EXISTS {_PLATFORM_STATUS_INDEX}")
    op.execute(f"ALTER TABLE platform_connections DROP CONSTRAINT IF EXISTS {_STATUS_REASON_CHECK}")
    op.execute("ALTER TABLE community_connection_access DROP COLUMN IF EXISTS enabled")
    op.execute(
        """
        ALTER TABLE platform_connections
            DROP COLUMN IF EXISTS status_reason,
            DROP COLUMN IF EXISTS expires_at,
            DROP COLUMN IF EXISTS resource_metadata
        """
    )
