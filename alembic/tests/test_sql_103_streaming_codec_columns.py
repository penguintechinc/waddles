"""`config/postgres/migrations/103_streaming_codec_columns.sql` -- static checks, a Rust-entity drift guard, and a real-Postgres run.

`streaming_configs` / `streaming_targets` are created by the legacy SQL migration system
(`079_svc_streaming.sql`), NOT by Alembic, so the codec/protocol columns svc-streaming reads
live in a numbered SQL migration next to them. An Alembic revision doing the same `ALTER TABLE`
fails a fresh `alembic upgrade head` (the table does not exist in Alembic's schema); the
regression guard below keeps it from coming back. Layers, cheapest first:

1. **Static** (no database): idempotent `ADD COLUMN IF NOT EXISTS` clauses, defaults, CHECK
   vocabularies, column comments, and no Alembic revision touching the streaming tables.
2. **Drift guard** (static parse): every codec/protocol column the Rust SeaORM entities
   (`core/svc_streaming/src/db/entities/`) select is created by 079 or 103.
3. **Real Postgres** (ephemeral container, skipped without docker): the real 079 is applied,
   pre-existing rows inserted, then 103 run twice -- proving defaults backfill old rows to today's
   behaviour (h264 / copy / rtmp), CHECKs reject unknown vocabulary, and a re-run is a no-op.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, empty_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SQL_DIR = _REPO_ROOT / "config" / "postgres" / "migrations"
_SQL_079 = _SQL_DIR / "079_svc_streaming.sql"
_SQL_103 = _SQL_DIR / "103_streaming_codec_columns.sql"
_ALEMBIC_VERSIONS = _REPO_ROOT / "alembic" / "versions"
_ENTITY_DIR = _REPO_ROOT / "core" / "svc_streaming" / "src" / "db" / "entities"

#: Columns migration 103 adds, per table -- the single source the tests below compare against.
_ADDED_COLUMNS = {
    "streaming_configs": {"video_codec", "audio_codec"},
    "streaming_targets": {"protocol", "video_codec", "audio_codec"},
}


def _sql_103() -> str:
    """The migration's text."""
    return _SQL_103.read_text(encoding="utf-8")


def _executable_sql() -> str:
    """Migration 103 with `--` comment lines stripped, so asserts see only real statements."""
    return "\n".join(
        line for line in _sql_103().splitlines() if not line.lstrip().startswith("--")
    )


class TestStaticSql:
    """The DDL, clause by clause."""

    def test_adds_config_codec_columns_with_todays_defaults(self) -> None:
        sql = _executable_sql()
        assert "ALTER TABLE streaming_configs" in sql
        assert (
            "ADD COLUMN IF NOT EXISTS video_codec VARCHAR(8) NOT NULL DEFAULT 'h264'" in sql
        )
        assert (
            "ADD COLUMN IF NOT EXISTS audio_codec VARCHAR(8) NOT NULL DEFAULT 'copy'" in sql
        )

    def test_adds_target_protocol_and_nullable_overrides(self) -> None:
        sql = _executable_sql()
        assert "ALTER TABLE streaming_targets" in sql
        assert (
            "ADD COLUMN IF NOT EXISTS protocol VARCHAR(8) NOT NULL DEFAULT 'rtmp'" in sql
        )
        # Overrides are nullable (NULL = inherit): no NOT NULL / DEFAULT on them.
        overrides = re.findall(
            r"ADD COLUMN IF NOT EXISTS (video_codec|audio_codec) VARCHAR\(8\)([^,;\n]*)", sql
        )
        nullable = [clause for _, clause in overrides if "NOT NULL" not in clause]
        assert len(nullable) == 2

    def test_check_constraints_pin_the_vocabulary(self) -> None:
        sql = _executable_sql()
        assert "CHECK (video_codec IN ('h264', 'h265', 'av1'))" in sql
        assert "CHECK (audio_codec IN ('copy', 'aac', 'opus'))" in sql
        assert "CHECK (protocol IN ('rtmp', 'srt'))" in sql

    def test_every_add_is_idempotent(self) -> None:
        sql = _executable_sql()
        assert sql.count("ADD COLUMN") == sql.count("ADD COLUMN IF NOT EXISTS") == 5

    def test_documents_every_new_column(self) -> None:
        sql = _executable_sql()
        for table, columns in _ADDED_COLUMNS.items():
            for column in columns:
                assert f"COMMENT ON COLUMN {table}.{column}" in sql

    def test_numbered_after_the_migration_that_creates_the_tables(self) -> None:
        """103 must sort after 079 (the CREATE TABLE) in the replay order."""
        assert _SQL_079.name < _SQL_103.name
        assert len(list(_SQL_DIR.glob("103_*.sql"))) == 1, "duplicate 103 prefix"


class TestNoAlembicRevisionTouchesStreamingTables:
    """Regression: gh-799 -- an Alembic `ALTER TABLE streaming_*` fails a fresh `alembic upgrade head`."""

    def test_no_alembic_version_alters_the_streaming_tables(self) -> None:
        versions = sorted(_ALEMBIC_VERSIONS.glob("*.py"))
        assert versions, "no alembic revisions examined -- wrong path?"
        offenders = [
            path.name
            for path in versions
            if re.search(
                r"ALTER\s+TABLE\s+streaming_(configs|targets|sessions)",
                path.read_text(encoding="utf-8"),
                re.IGNORECASE,
            )
        ]
        assert not offenders, (
            "streaming_* tables are owned by config/postgres/migrations/079_svc_streaming.sql, "
            f"not Alembic -- move the ALTER to a numbered SQL migration: {offenders}"
        )


def _entity_columns(entity_file: str) -> set[str]:
    """Field names declared on a SeaORM `Model` struct (static parse of the Rust source)."""
    source = (_ENTITY_DIR / entity_file).read_text(encoding="utf-8")
    body = re.search(r"pub struct Model \{(.*?)\n\}", source, re.DOTALL)
    assert body is not None, f"no `pub struct Model` in {entity_file}"
    return set(re.findall(r"^\s*pub (\w+):", body.group(1), re.MULTILINE))


_ENTITIES = [
    ("streaming_configs", "streaming_config.rs"),
    ("streaming_targets", "streaming_target.rs"),
]


class TestNoDriftAgainstRustEntities:
    """The columns svc-streaming selects must exist after migrating."""

    @pytest.mark.parametrize(("table", "entity_file"), _ENTITIES)
    def test_every_new_column_is_declared_on_the_entity(
        self, table: str, entity_file: str
    ) -> None:
        entity_fields = _entity_columns(entity_file)
        assert _ADDED_COLUMNS[table] <= entity_fields, (
            f"{entity_file} no longer declares {_ADDED_COLUMNS[table] - entity_fields}"
        )

    @pytest.mark.parametrize(("table", "entity_file"), _ENTITIES)
    def test_entity_columns_are_all_in_079_or_103(
        self, table: str, entity_file: str
    ) -> None:
        sql_079 = _SQL_079.read_text(encoding="utf-8")
        create = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql_079, re.DOTALL
        )
        assert create is not None
        baseline = set(re.findall(r"^\s+(\w+)\s+[A-Z]", create.group(1), re.MULTILINE))
        missing = _entity_columns(entity_file) - baseline - _ADDED_COLUMNS[table]
        assert not missing, (
            f"{entity_file} selects columns no migration creates: {sorted(missing)}"
        )


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """A bare Postgres 17 with the real 079 DDL applied and two pre-existing rows."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with empty_postgres("sql103-streaming-codec") as db:
        conn = psycopg2.connect(db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            # 079 references `communities` and seeds `token_products`; stand both in.
            cur.execute("CREATE TABLE communities (id SERIAL PRIMARY KEY)")
            cur.execute(
                "CREATE TABLE token_products (key TEXT PRIMARY KEY, name TEXT, unit TEXT, "
                "price_cents INTEGER, tokens_granted INTEGER, active BOOLEAN)"
            )
            cur.execute(_SQL_079.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO communities DEFAULT VALUES")
            cur.execute(
                "INSERT INTO streaming_configs (community_id, source_url) VALUES (1, 'sk_old')"
            )
            cur.execute(
                "INSERT INTO streaming_targets (config_id, platform, forward_url) "
                'VALUES (1, \'twitch\', \'{"source":"env","var":"X"}\')'
            )
        conn.close()
        yield db


def _apply_103(db: PgTestDatabase) -> None:
    """Run migration 103 against `db` for real."""
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(_sql_103())
    conn.close()


def _columns(db: PgTestDatabase, table: str) -> set[str]:
    """Current column names of `table`."""
    conn = psycopg2.connect(db.dsn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        names = {row[0] for row in cur.fetchall()}
    conn.close()
    return names


@requires_docker
class TestRealPostgres:
    """Apply / backfill / constraint / idempotency against genuine Postgres (tests run in order)."""

    def test_columns_absent_before_103(self, pg_db: PgTestDatabase) -> None:
        for table, added in _ADDED_COLUMNS.items():
            assert not (added & _columns(pg_db, table))

    def test_apply_backfills_existing_rows_to_todays_behaviour(
        self, pg_db: PgTestDatabase
    ) -> None:
        _apply_103(pg_db)
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT video_codec, audio_codec FROM streaming_configs WHERE source_url = 'sk_old'"
            )
            assert cur.fetchone() == ("h264", "copy")
            cur.execute(
                "SELECT protocol, video_codec, audio_codec FROM streaming_targets "
                "WHERE platform = 'twitch'"
            )
            assert cur.fetchone() == ("rtmp", None, None)
        conn.close()

    def test_apply_added_exactly_the_documented_columns(self, pg_db: PgTestDatabase) -> None:
        for table, added in _ADDED_COLUMNS.items():
            assert added <= _columns(pg_db, table)

    def test_check_constraints_reject_unknown_vocabulary(self, pg_db: PgTestDatabase) -> None:
        conn = psycopg2.connect(pg_db.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            for statement in (
                "UPDATE streaming_configs SET video_codec = 'vp9'",
                "UPDATE streaming_configs SET audio_codec = 'flac'",
                "UPDATE streaming_targets SET protocol = 'whip'",
                "UPDATE streaming_targets SET video_codec = 'mpeg2'",
                "UPDATE streaming_targets SET audio_codec = 'mp3'",
            ):
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(statement)
            # The supported vocabulary is accepted, and NULL (= inherit) on target overrides.
            cur.execute("UPDATE streaming_configs SET video_codec = 'av1', audio_codec = 'opus'")
            cur.execute(
                "UPDATE streaming_targets SET protocol = 'srt', video_codec = 'h265', "
                "audio_codec = 'aac'"
            )
            cur.execute("UPDATE streaming_targets SET video_codec = NULL, audio_codec = NULL")
        conn.close()

    def test_apply_is_idempotent_and_preserves_data(self, pg_db: PgTestDatabase) -> None:
        _apply_103(pg_db)
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            # A re-run must not reset values written since the first run.
            cur.execute("SELECT video_codec, audio_codec FROM streaming_configs")
            assert cur.fetchone() == ("av1", "opus")
        conn.close()
