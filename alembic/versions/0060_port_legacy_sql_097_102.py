"""Port legacy SQL migrations 097, 099, 100, 101, 102 (+ 098's ledger row) into Alembic.

**The gap this closes.** `config/postgres/migrations/NNN_*.sql` files are replayed
only by `0001_baseline_from_sql_migrations` on a genuinely FRESH database; once
`schema_migrations` has rows the baseline no-ops forever, so a new numbered SQL
file never reaches an existing DB (alpha, beta, prod) by itself. 084-096 were
bridged by `0014_wave1a_bundle_seeds`; 097-102 landed afterwards with no bridge:

  - 097 `reputation_tenant` (re-scope reputation from GLOBAL to TENANT; drops
    `reputation_global`, backfills, grants `mod_core_reputation` if it exists);
  - 098 `event_sync_enabled` -- its DDL is already applied by
    `0039_event_sync_enabled`, so only the ledger row is written here;
  - 099 `community_polls.created_by` nullable + `created_by_hash` + CHECK + index;
  - 100 `overlay_view_credentials` (+ hashed backfill from `community_overlay_tokens`);
  - 101 `overlay_images`;
  - 102 `caption_events` Rust-port reconcile (BIGINT ids, `user_ref`, NOT NULL).

**Idempotent on both paths.** Fresh DB: 0001's replay already applied every file, so
each statement below is a guarded no-op (`IF NOT EXISTS`, `to_regclass`, a
`pg_constraint` probe for 099's CHECK). Existing DB: the objects are absent and are
created. Statements are the SQL files' own, only wrapped so a re-run cannot fail.

**Two environments, one rule.** A DB with a populated `schema_migrations` ledger is a
real legacy DB: its pre-097 tables MUST exist (a missing one raises, never a silent
skip) and the ledger rows are written. A DB without that ledger is the minimal
bootstrap `alembic/tests/pg_docker.py` + the PR `alembic upgrade head` job stamp at
`0019_kick_app` (no legacy baseline at all): only the self-contained DDL is applied,
the parts that need legacy-only tables are skipped with a WARNING, and no ledger row is
written (there is no ledger to write to, and claiming a skipped file applied would lie).

**Downgrade removes the ledger rows only.** The DDL is not reverted: on a fresh DB it
belongs to the baseline replay, and dropping `reputation_tenant` (or re-creating the
dropped `reputation_global`) would destroy data. Re-upgrading re-applies idempotently.

**Follow-ups.** 103 (`103_streaming_codec_columns.sql`, PR #799) is NOT ported here --
it is not on this branch's base. Port it as a new revision once #799 lands, then drop
it from `PENDING_PORT` in `alembic/tests/test_legacy_sql_ownership.py`. Per-service DB
roles (0055) skip catalog tables that do not exist yet, so after `alembic upgrade head`
the deploy-time `scripts/db/service_roles.py reconcile --strict` is what grants the new
097/100/101/102 tables -- see docs/DATABASE.md "SQL vs Alembic ownership".

Revision ID: 0060_port_legacy_sql_097_102
Revises: 0059_suppression_records
Create Date: 2026-10-10
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision = "0060_port_legacy_sql_097_102"
down_revision = "0059_suppression_records"
branch_labels = None
depends_on = None

_LOG = logging.getLogger("alembic.runtime.migration")

#: `schema_migrations.version` values (SQL filename minus `.sql`, the shape
#: 0001_baseline and 0014 write) this revision bookkeeps. 098 is ledger-only.
LEGACY_VERSIONS: tuple[str, ...] = (
    "097_reputation_tenant_scope",
    "098_event_sync_enabled",
    "099_poll_bundle_actor_hash",
    "100_overlay_view_credentials",
    "101_overlay_images",
    "102_caption_events_rust_port",
)

#: Pre-097 legacy tables the ported DDL reads/alters/FKs; all must exist on a real DB.
_REQUIRED_LEGACY_TABLES: tuple[str, ...] = (
    "tenants",
    "hub_users",
    "communities",
    "community_polls",
    "community_overlay_tokens",
)

_SQL_097 = (
    """
    CREATE TABLE IF NOT EXISTS reputation_tenant (
        tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        hub_user_id INTEGER NOT NULL REFERENCES hub_users(id) ON DELETE CASCADE,
        score INTEGER NOT NULL DEFAULT 600,
        total_events INTEGER NOT NULL DEFAULT 0,
        last_event_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (tenant_id, hub_user_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_reputation_tenant_score
        ON reputation_tenant(tenant_id, score DESC)
    """,
    # One-time carry-forward backfill, then the hard replacement drop. Both only while
    # reputation_global still exists (fresh DB: 0001's replay already did this).
    """
    DO $port$
    BEGIN
        IF to_regclass('public.reputation_global') IS NULL THEN
            RAISE NOTICE '097: reputation_global already superseded, backfill/drop skipped';
            RETURN;
        END IF;
        INSERT INTO reputation_tenant
            (tenant_id, hub_user_id, score, total_events, last_event_at, created_at, updated_at)
        SELECT DISTINCT
            c.tenant_id, rg.hub_user_id, rg.score, rg.total_events,
            rg.last_event_at, rg.created_at, rg.updated_at
        FROM reputation_global rg
        JOIN reputation_events re ON re.hub_user_id = rg.hub_user_id
        JOIN communities c ON c.id = re.community_id
        ON CONFLICT (tenant_id, hub_user_id) DO NOTHING;
        DROP TABLE reputation_global;
    END
    $port$
    """,
    """
    DO $port$
    BEGIN
        IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'mod_core_reputation') THEN
            GRANT SELECT, INSERT, UPDATE ON reputation_tenant TO mod_core_reputation;
        END IF;
    END
    $port$
    """,
)

#: Needs `community_polls` (legacy 028) -- applied only on a real legacy DB.
_SQL_099 = (
    "ALTER TABLE community_polls ALTER COLUMN created_by DROP NOT NULL",
    "ALTER TABLE community_polls ADD COLUMN IF NOT EXISTS created_by_hash VARCHAR(64)",
    """
    COMMENT ON COLUMN community_polls.created_by_hash IS
        'SHA-256 hash of the chat actor that created this poll via bundles/python/poll, '
        'when created_by (a real hub_users.id) could not be resolved. Mutually '
        'exclusive with created_by in practice, never both NULL.'
    """,
    # ADD CONSTRAINT has no IF NOT EXISTS -- probe pg_constraint instead.
    """
    DO $port$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.community_polls'::regclass
              AND conname = 'community_polls_creator_identity_chk'
        ) THEN
            ALTER TABLE community_polls
                ADD CONSTRAINT community_polls_creator_identity_chk
                CHECK (created_by IS NOT NULL OR created_by_hash IS NOT NULL);
        END IF;
    END
    $port$
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_community_polls_created_by_hash
        ON community_polls (created_by_hash)
        WHERE created_by_hash IS NOT NULL
    """,
)

_SQL_100_DDL = (
    """
    CREATE TABLE IF NOT EXISTS overlay_view_credentials (
        id                 BIGSERIAL PRIMARY KEY,
        community_id       BIGINT NOT NULL UNIQUE REFERENCES communities(id) ON DELETE CASCADE,
        key_hash           TEXT NOT NULL UNIQUE,
        previous_key_hash  TEXT,
        is_active          BOOLEAN NOT NULL DEFAULT TRUE,
        rotated_at         TIMESTAMPTZ,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_overlay_view_credentials_key_hash
        ON overlay_view_credentials (key_hash)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_overlay_view_credentials_previous_key_hash
        ON overlay_view_credentials (previous_key_hash)
        WHERE previous_key_hash IS NOT NULL
    """,
)

#: Hashes the legacy plaintext keys forward (needs `community_overlay_tokens`, legacy only).
_SQL_100_BACKFILL = """
    INSERT INTO overlay_view_credentials
        (community_id, key_hash, previous_key_hash, is_active, rotated_at, created_at, updated_at)
    SELECT
        community_id,
        encode(sha256(overlay_key::bytea), 'hex'),
        CASE WHEN previous_key IS NOT NULL THEN encode(sha256(previous_key::bytea), 'hex') END,
        is_active,
        rotated_at,
        created_at,
        updated_at
    FROM community_overlay_tokens
    ON CONFLICT (community_id) DO NOTHING
"""

_SQL_101 = (
    """
    CREATE TABLE IF NOT EXISTS overlay_images (
        id              BIGSERIAL PRIMARY KEY,
        community_id    BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
        asset_id        UUID NOT NULL UNIQUE,
        object_key      TEXT NOT NULL,
        content_type    TEXT NOT NULL,
        size_bytes      BIGINT NOT NULL,
        sha256          TEXT NOT NULL,
        position_x      INTEGER,
        position_y      INTEGER,
        width           INTEGER,
        height          INTEGER,
        duration_ms     BIGINT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_overlay_images_community_id
        ON overlay_images (community_id)
    """,
)

#: Creates the final shape when absent, otherwise reconciles the 007 shape to it.
_SQL_102 = (
    """
    CREATE TABLE IF NOT EXISTS caption_events (
        id                  BIGSERIAL PRIMARY KEY,
        community_id        BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
        user_ref            UUID,
        platform            VARCHAR(50) NOT NULL,
        original_message    TEXT NOT NULL,
        translated_message  TEXT,
        detected_language   VARCHAR(10),
        target_language     VARCHAR(10),
        confidence_score    DOUBLE PRECISION,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "ALTER TABLE caption_events ADD COLUMN IF NOT EXISTS user_ref UUID",
    "ALTER TABLE caption_events ALTER COLUMN id TYPE BIGINT",
    "ALTER SEQUENCE IF EXISTS caption_events_id_seq AS BIGINT",
    "ALTER TABLE caption_events ALTER COLUMN community_id TYPE BIGINT",
    # A community-less row is unreadable (every read is community-scoped); removing it
    # lets the tenant-scoping column be NOT NULL. Same intent as the SQL file.
    "DELETE FROM caption_events WHERE community_id IS NULL",
    "ALTER TABLE caption_events ALTER COLUMN community_id SET NOT NULL",
    "UPDATE caption_events SET created_at = NOW() WHERE created_at IS NULL",
    "ALTER TABLE caption_events ALTER COLUMN created_at SET DEFAULT NOW()",
    "ALTER TABLE caption_events ALTER COLUMN created_at SET NOT NULL",
    """
    ALTER TABLE caption_events
        ALTER COLUMN confidence_score TYPE DOUBLE PRECISION
        USING confidence_score::double precision
    """,
    # Legacy PII column (007): keep for the Python writer until cutover, never require it.
    """
    DO $port$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'caption_events'
              AND column_name = 'username'
        ) THEN
            ALTER TABLE caption_events ALTER COLUMN username DROP NOT NULL;
            COMMENT ON COLUMN caption_events.username IS
                'DEPRECATED legacy PII column, written only by the Python browser_source module; dropped at the Python caption-path cutover';
        END IF;
    END
    $port$
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_caption_events_recent
        ON caption_events (community_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_caption_events_created_at
        ON caption_events (created_at)
    """,
    """
    COMMENT ON COLUMN caption_events.user_ref IS
        'Tenant-tokenized UUID of the message author -- never a username or display name'
    """,
)


def _run(statements: tuple[str, ...]) -> None:
    """Execute each SQL statement in order inside the migration transaction."""
    for statement in statements:
        op.execute(sa.text(statement))


def _legacy_ledger_present(conn: Connection) -> bool:
    """True when `schema_migrations` exists with at least one row (0001's own test).

    That is a real legacy database; its absence is the minimal CI/test bootstrap.
    """
    exists = conn.execute(
        sa.text("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
    ).scalar()
    if not exists:
        return False
    return bool(conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM schema_migrations)")).scalar())


def _ledger_has_version_column(conn: Connection) -> bool:
    """True when `schema_migrations` is the `version`-keyed shape 0001/0014 write."""
    return bool(
        conn.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'schema_migrations' "
                "AND column_name = 'version')"
            )
        ).scalar()
    )


def _require_legacy_tables(conn: Connection) -> None:
    """Fail loudly if a real legacy DB lacks a pre-097 table (never a silent skip)."""
    missing = [
        table
        for table in _REQUIRED_LEGACY_TABLES
        if not conn.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table}"}
        ).scalar()
    ]
    if missing:
        raise RuntimeError(
            f"0060: legacy schema_migrations ledger is populated but required pre-097 "
            f"table(s) are missing: {missing}. Refusing to bookkeep 097-102 as applied "
            f"on a DB whose baseline is incomplete."
        )


def _record_applied(conn: Connection) -> int:
    """Insert the legacy ledger rows (ON CONFLICT DO NOTHING); return how many were new."""
    if not _ledger_has_version_column(conn):
        raise RuntimeError(
            "0060: schema_migrations has no `version` column (the psql-runner "
            "`migration_file` shape?) -- 0001/0014 key the ledger on `version`; "
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
    """Apply 097/099/100/101/102 idempotently and ledger 097-102 on a real legacy DB."""
    conn = op.get_bind()
    legacy = _legacy_ledger_present(conn)
    if legacy:
        _require_legacy_tables(conn)
    else:
        _LOG.warning(
            "0060: no populated schema_migrations ledger (minimal/CI bootstrap) -- skipping "
            "099 and the 100 backfill (need legacy tables) and not writing ledger rows"
        )

    _run(_SQL_097)
    if legacy:
        _run(_SQL_099)
    _run(_SQL_100_DDL)
    if legacy:
        op.execute(sa.text(_SQL_100_BACKFILL))
    _run(_SQL_101)
    _run(_SQL_102)

    if legacy:
        new_rows = _record_applied(conn)
        _LOG.warning(
            "0060: ported legacy SQL 097-102; ledger rows newly written %d of %d "
            "(the rest were already recorded by the baseline replay)",
            new_rows,
            len(LEGACY_VERSIONS),
        )


def downgrade() -> None:
    """Remove the ledger rows only; the DDL/data is deliberately left in place (see docstring)."""
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
