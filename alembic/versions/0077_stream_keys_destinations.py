"""Hash-only stream keys + encrypted restream destinations for hub-api's stream admin API.

**Stream key.** ``streaming_configs`` (legacy SQL 079, svc-streaming's table) had no key
column at all: svc-streaming's ingest-auth matched the presented key against the plaintext
``source_url``. This adds ``stream_key_hash`` (hex SHA-256 of a 256-bit CSPRNG key, UNIQUE),
``stream_key_fingerprint`` (first 8 hex chars of the hash, for display only) and
``stream_key_rotated_at``. The plaintext is shown once at create/rotate and never stored.
svc-streaming now authorizes by hashing the presented key and matching ``stream_key_hash``.

**Destinations.** ``streaming_destinations`` is hub-api-owned: one row per restream target
with the third-party RTMP URL and stream key sealed (AES-256-GCM, row-bound AAD) in
``rtmp_url_enc`` / ``stream_key_enc``. ``display_url`` holds only scheme://host[:port] so the
UI can list a destination without decrypting anything.

Same two-environment rule as 0061: a populated ``schema_migrations`` ledger means a real
legacy DB where ``streaming_configs`` MUST exist (missing raises); without the ledger (the
minimal CI bootstrap) the DDL is skipped with a WARNING. Downgrade drops only the destinations table;
the hash columns are retained so minted keys survive a rollback.

Revision ID: 0077_stream_keys_destinations
Revises: 0076_suggestion_community_id
Create Date: 2026-11-06
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision = "0077_stream_keys_destinations"
down_revision = "0076_suggestion_community_id"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")

_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE streaming_configs
        ADD COLUMN IF NOT EXISTS stream_key_hash VARCHAR(64),
        ADD COLUMN IF NOT EXISTS stream_key_fingerprint VARCHAR(8),
        ADD COLUMN IF NOT EXISTS stream_key_rotated_at TIMESTAMPTZ
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_streaming_configs_stream_key_hash
        ON streaming_configs (stream_key_hash) WHERE stream_key_hash IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS streaming_destinations (
        id SERIAL PRIMARY KEY,
        public_id VARCHAR(36) NOT NULL UNIQUE,
        config_id INTEGER NOT NULL REFERENCES streaming_configs(id) ON DELETE CASCADE,
        platform VARCHAR(20) NOT NULL
            CHECK (platform IN ('twitch', 'youtube', 'facebook', 'kick', 'custom')),
        display_url VARCHAR(300) NOT NULL,
        rtmp_url_enc TEXT NOT NULL,
        stream_key_enc TEXT NOT NULL,
        max_resolution VARCHAR(8) NOT NULL DEFAULT '1080p'
            CHECK (max_resolution IN ('720p', '1080p', '1440p', '2160p')),
        video_codec VARCHAR(8) NOT NULL DEFAULT 'h264' CHECK (video_codec = 'h264'),
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        force_cut BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_streaming_destinations_config
        ON streaming_destinations (config_id)
    """,
    """
    COMMENT ON COLUMN streaming_destinations.stream_key_enc IS
        'AES-256-GCM sealed third-party stream key (AAD = config_id:public_id); never returned by the API'
    """,
)


def _legacy_ledger_present(conn: Connection) -> bool:
    """True when ``schema_migrations`` exists with at least one row (0001's own test)."""
    exists = conn.execute(
        sa.text("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
    ).scalar()
    if not exists:
        return False
    return bool(
        conn.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM schema_migrations)")
        ).scalar()
    )


def upgrade() -> None:
    """Add the key-hash columns and the destinations table (skipped on the minimal bootstrap)."""
    conn = op.get_bind()
    present = bool(
        conn.execute(
            sa.text("SELECT to_regclass('public.streaming_configs') IS NOT NULL")
        ).scalar()
    )
    if not present:
        if _legacy_ledger_present(conn):
            raise RuntimeError(
                "0077: legacy ledger is populated but streaming_configs (legacy SQL 079) is "
                "missing; refusing to continue on an incomplete baseline."
            )
        _LOG.warning(
            "0077: streaming_configs absent (minimal/CI bootstrap) -- skipping DDL"
        )
        return
    for statement in _STATEMENTS:
        op.execute(sa.text(statement))


def downgrade() -> None:
    """Drop the destinations table; the key-hash columns/index are retained (hashes only), the
    same convention as 0060/0061, so a rollback never invalidates minted stream keys."""
    op.execute(sa.text("DROP TABLE IF EXISTS streaming_destinations"))
