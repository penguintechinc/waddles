"""Port legacy SQL 103 (streaming codec/protocol columns) into Alembic.

**The gap this closes.** `config/postgres/migrations/103_streaming_codec_columns.sql`
is replayed only by `0001_baseline_from_sql_migrations` on a genuinely FRESH database, so
it never reached an existing DB (alpha, beta, prod): `streaming_configs.video_codec` /
`audio_codec` and `streaming_targets.protocol` / `video_codec` / `audio_codec` were
missing there, leaving svc-streaming's HEVC/AV1 + SRT target selection broken. 0060 ported
097-102 and explicitly left 103 for a follow-up; this is that follow-up.

**Idempotent on both paths.** Fresh DB: 0001's replay already applied 103, so every
`ADD COLUMN IF NOT EXISTS` is a no-op. Existing DB: the columns are absent and are added
with the SQL file's own types, defaults and CHECK constraints (every default reproduces the
pre-103 behaviour: H.264 / copy / RTMP, so existing rows are unaffected).

**Two environments, one rule** (same as 0060). `streaming_configs`/`streaming_targets` are
created by legacy SQL 079, not Alembic. A DB with a populated `schema_migrations` ledger is a
real legacy DB: those tables MUST exist (a missing one raises, never a silent skip) and the
ledger row is written. A DB without that ledger is the minimal CI bootstrap
(`alembic/tests/pg_docker.py`, stamped at `0019_kick_app`, no legacy baseline): there is no
table to alter, so the DDL is skipped with a WARNING and no ledger row is invented.

**Downgrade** drops the five columns (their CHECK constraints go with them) and removes the
ledger row. Unlike 0060 the columns are new, additive, and carry no pre-existing data to
protect; re-upgrading re-adds them idempotently.

Revision ID: 0061_port_legacy_sql_103_codec
Revises: 0060_port_legacy_sql_097_102
Create Date: 2026-10-11
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision = "0061_port_legacy_sql_103_codec"
down_revision = "0060_port_legacy_sql_097_102"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")

#: `schema_migrations.version` values (SQL filename minus `.sql`) this revision bookkeeps.
LEGACY_VERSIONS: tuple[str, ...] = ("103_streaming_codec_columns",)

#: Legacy (079) tables the ported DDL alters; both must exist on a real legacy DB.
_REQUIRED_LEGACY_TABLES: tuple[str, ...] = ("streaming_configs", "streaming_targets")

_SQL_103 = (
    """
    ALTER TABLE streaming_configs
        ADD COLUMN IF NOT EXISTS video_codec VARCHAR(8) NOT NULL DEFAULT 'h264'
            CHECK (video_codec IN ('h264', 'h265', 'av1')),
        ADD COLUMN IF NOT EXISTS audio_codec VARCHAR(8) NOT NULL DEFAULT 'copy'
            CHECK (audio_codec IN ('copy', 'aac', 'opus'))
    """,
    """
    ALTER TABLE streaming_targets
        ADD COLUMN IF NOT EXISTS protocol VARCHAR(8) NOT NULL DEFAULT 'rtmp'
            CHECK (protocol IN ('rtmp', 'srt')),
        ADD COLUMN IF NOT EXISTS video_codec VARCHAR(8)
            CHECK (video_codec IN ('h264', 'h265', 'av1')),
        ADD COLUMN IF NOT EXISTS audio_codec VARCHAR(8)
            CHECK (audio_codec IN ('copy', 'aac', 'opus'))
    """,
    """
    COMMENT ON COLUMN streaming_configs.video_codec IS
        'Output video codec when transcode_enabled (h264/h265/av1); pipeline/codec.rs::VideoFamily'
    """,
    """
    COMMENT ON COLUMN streaming_configs.audio_codec IS
        'Output audio handling (copy/aac/opus); pipeline/codec.rs::AudioChoice'
    """,
    """
    COMMENT ON COLUMN streaming_targets.protocol IS
        'Push protocol (rtmp/srt): picks the FLV vs MPEG-TS container; rtmps:// URLs are protocol=rtmp'
    """,
    """
    COMMENT ON COLUMN streaming_targets.video_codec IS
        'Per-target video codec override; NULL = RTMP:h264, SRT:config video_codec'
    """,
    """
    COMMENT ON COLUMN streaming_targets.audio_codec IS
        'Per-target audio codec override; NULL = config audio_codec'
    """,
)

_SQL_DOWNGRADE = (
    """
    ALTER TABLE IF EXISTS streaming_targets
        DROP COLUMN IF EXISTS audio_codec,
        DROP COLUMN IF EXISTS video_codec,
        DROP COLUMN IF EXISTS protocol
    """,
    """
    ALTER TABLE IF EXISTS streaming_configs
        DROP COLUMN IF EXISTS audio_codec,
        DROP COLUMN IF EXISTS video_codec
    """,
)


def _legacy_ledger_present(conn: Connection) -> bool:
    """True when `schema_migrations` exists with at least one row (0001's own test)."""
    exists = conn.execute(
        sa.text("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
    ).scalar()
    if not exists:
        return False
    return bool(conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM schema_migrations)")).scalar())


def _ledger_has_version_column(conn: Connection) -> bool:
    """True when `schema_migrations` is the `version`-keyed shape 0001/0014/0060 write."""
    return bool(
        conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'schema_migrations' "
                "AND column_name = 'version')"
            )
        ).scalar()
    )


def _missing_legacy_tables(conn: Connection) -> list[str]:
    """Names of the 079 streaming tables that do not exist in `public`."""
    return [
        table
        for table in _REQUIRED_LEGACY_TABLES
        if not conn.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}
        ).scalar()
    ]


def _record_applied(conn: Connection) -> int:
    """Insert the legacy ledger row (ON CONFLICT DO NOTHING); return how many were new."""
    if not _ledger_has_version_column(conn):
        raise RuntimeError(
            "0061: schema_migrations has no `version` column (the psql-runner "
            "`migration_file` shape?) -- 0001/0014/0060 key the ledger on `version`; "
            "refusing to guess a mapping."
        )
    inserted = 0
    for version in LEGACY_VERSIONS:
        result = conn.execute(
            sa.text("INSERT INTO schema_migrations (version) VALUES (:v) ON CONFLICT DO NOTHING"),
            {"v": version},
        )
        inserted += result.rowcount
    return inserted


def upgrade() -> None:
    """Add the 103 codec/protocol columns idempotently; ledger 103 on a real legacy DB."""
    conn = op.get_bind()
    legacy = _legacy_ledger_present(conn)
    missing = _missing_legacy_tables(conn)
    if missing and legacy:
        raise RuntimeError(
            f"0061: legacy schema_migrations ledger is populated but streaming table(s) "
            f"{missing} (legacy SQL 079) are missing. Refusing to bookkeep 103 as applied "
            f"on a DB whose baseline is incomplete."
        )
    if missing:
        _LOG.warning(
            "0061: no populated schema_migrations ledger and streaming table(s) %s absent "
            "(minimal/CI bootstrap) -- skipping 103 and not writing a ledger row",
            missing,
        )
        return

    for statement in _SQL_103:
        op.execute(sa.text(statement))

    if legacy:
        new_rows = _record_applied(conn)
        _LOG.warning(
            "0061: ported legacy SQL 103; ledger rows newly written %d of %d "
            "(the rest were already recorded by the baseline replay)",
            new_rows,
            len(LEGACY_VERSIONS),
        )


def downgrade() -> None:
    """Drop the five 103 columns and remove the ledger row (down to 0060_port_legacy_sql_097_102)."""
    for statement in _SQL_DOWNGRADE:
        op.execute(sa.text(statement))
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
    ).scalar()
    if not exists or not _ledger_has_version_column(conn):
        return
    conn.execute(
        sa.text("DELETE FROM schema_migrations WHERE version = ANY(:versions)"),
        {"versions": list(LEGACY_VERSIONS)},
    )
