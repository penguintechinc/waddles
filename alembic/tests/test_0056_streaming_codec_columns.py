"""`0056_streaming_codec_columns` -- mocked-SQL assertions, a Rust-entity drift guard, and a real-Postgres round trip.

Three layers, cheapest first:

1. **Emitted SQL** (mocked `alembic.op.execute`, no database): the exact
   `ADD COLUMN IF NOT EXISTS` clauses, defaults, and CHECK vocabularies.
2. **Drift guard** (static parse, no database): every codec/protocol column
   the Rust SeaORM entities (`core/svc_streaming/src/db/entities/`) read must
   be created by this migration -- a column the entity selects but no
   migration adds fails every `streaming_*` query at runtime.
3. **Real Postgres** (ephemeral container, skipped without docker): the real
   `079_svc_streaming.sql` is applied, existing rows are inserted, then
   `upgrade()` / `downgrade()` / `upgrade()` run against it -- proving the
   defaults backfill old rows to today's behaviour (h264 / copy / rtmp), the
   CHECK constraints reject unknown vocabulary, and the revision is
   idempotent and reversible.
"""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, empty_postgres

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _REPO_ROOT / "alembic" / "versions" / "0056_streaming_codec_columns.py"
)
_ENTITY_DIR = _REPO_ROOT / "core" / "svc_streaming" / "src" / "db" / "entities"
_SQL_079 = _REPO_ROOT / "config" / "postgres" / "migrations" / "079_svc_streaming.sql"

#: Columns this revision adds, per table -- the single source the tests below compare against.
_ADDED_COLUMNS = {
    "streaming_configs": {"video_codec", "audio_codec"},
    "streaming_targets": {"protocol", "video_codec", "audio_codec"},
}


def _load_migration() -> ModuleType:
    """Import the migration module by path (alembic version files are not a package)."""
    spec = importlib.util.spec_from_file_location(
        "migration_0056_streaming_codec_columns", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_sql(method: str) -> str:
    """Run `upgrade`/`downgrade` with `op.execute` mocked and return the SQL it emitted."""
    with patch("alembic.op.execute") as mock_execute:
        getattr(_load_migration(), method)()
    return "\n".join(call.args[0] for call in mock_execute.call_args_list)


class TestMigrationMetadata:
    """Revision chain facts a parallel PR can silently break."""

    def test_chains_off_the_current_head(self) -> None:
        """down_revision must be the head this was written against (re-point on renumber)."""
        migration = _load_migration()
        assert migration.revision == "0056_streaming_codec_columns"
        assert migration.down_revision == "0053_audit_events_hash_chain"

    def test_revision_id_fits_alembic_version_num_varchar32(self) -> None:
        """`alembic_version.version_num` is VARCHAR(32); a longer id errors on real Postgres."""
        assert len(_load_migration().revision) <= 32


class TestUpgradeSql:
    """The emitted DDL, clause by clause."""

    def test_adds_config_codec_columns_with_todays_defaults(self) -> None:
        sql = _migration_sql("upgrade")
        assert "ALTER TABLE streaming_configs" in sql
        assert (
            "ADD COLUMN IF NOT EXISTS video_codec VARCHAR(8) NOT NULL DEFAULT 'h264'"
            in sql
        )
        assert (
            "ADD COLUMN IF NOT EXISTS audio_codec VARCHAR(8) NOT NULL DEFAULT 'copy'"
            in sql
        )

    def test_adds_target_protocol_and_nullable_overrides(self) -> None:
        sql = _migration_sql("upgrade")
        assert "ALTER TABLE streaming_targets" in sql
        assert (
            "ADD COLUMN IF NOT EXISTS protocol VARCHAR(8) NOT NULL DEFAULT 'rtmp'"
            in sql
        )
        # Overrides are nullable (NULL = inherit): no NOT NULL / DEFAULT on them.
        overrides = re.findall(
            r"ADD COLUMN IF NOT EXISTS (video_codec|audio_codec) VARCHAR\(8\)([^,\n]*)",
            sql,
        )
        target_overrides = [
            clause for _, clause in overrides if "NOT NULL" not in clause
        ]
        assert len(target_overrides) == 2

    def test_check_constraints_pin_the_vocabulary(self) -> None:
        sql = _migration_sql("upgrade")
        assert "CHECK (video_codec IN ('h264', 'h265', 'av1'))" in sql
        assert "CHECK (audio_codec IN ('copy', 'aac', 'opus'))" in sql
        assert "CHECK (protocol IN ('rtmp', 'srt'))" in sql

    def test_every_add_is_idempotent(self) -> None:
        sql = _migration_sql("upgrade")
        assert sql.count("ADD COLUMN") == sql.count("ADD COLUMN IF NOT EXISTS") == 5

    def test_documents_every_new_column(self) -> None:
        sql = _migration_sql("upgrade")
        for table, columns in _ADDED_COLUMNS.items():
            for column in columns:
                assert f"COMMENT ON COLUMN {table}.{column}" in sql


class TestDowngradeSql:
    """The reverse DDL drops exactly what the upgrade added."""

    def test_drops_every_added_column(self) -> None:
        sql = _migration_sql("downgrade")
        assert sql.count("DROP COLUMN IF EXISTS") == 5
        for columns in _ADDED_COLUMNS.values():
            for column in columns:
                assert f"DROP COLUMN IF EXISTS {column}" in sql


def _entity_columns(entity_file: str) -> set[str]:
    """Field names declared on a SeaORM `Model` struct (static parse of the Rust source)."""
    source = (_ENTITY_DIR / entity_file).read_text(encoding="utf-8")
    body = re.search(r"pub struct Model \{(.*?)\n\}", source, re.DOTALL)
    assert body is not None, f"no `pub struct Model` in {entity_file}"
    return set(re.findall(r"^\s*pub (\w+):", body.group(1), re.MULTILINE))


class TestNoDriftAgainstRustEntities:
    """The columns svc-streaming selects must exist after migrating."""

    @pytest.mark.parametrize(
        ("table", "entity_file"),
        [
            ("streaming_configs", "streaming_config.rs"),
            ("streaming_targets", "streaming_target.rs"),
        ],
    )
    def test_every_new_entity_column_is_added_by_the_migration(
        self, table: str, entity_file: str
    ) -> None:
        entity_fields = _entity_columns(entity_file)
        assert _ADDED_COLUMNS[table] <= entity_fields, (
            f"{entity_file} no longer declares {_ADDED_COLUMNS[table] - entity_fields}"
        )

    @pytest.mark.parametrize(
        ("table", "entity_file"),
        [
            ("streaming_configs", "streaming_config.rs"),
            ("streaming_targets", "streaming_target.rs"),
        ],
    )
    def test_entity_columns_are_all_in_079_or_this_migration(
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
    with empty_postgres("0056-streaming-codec") as db:
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


def _run(db: PgTestDatabase, method: str) -> None:
    """Execute the migration's `upgrade`/`downgrade` against `db` for real."""
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    with conn.cursor() as cur, patch("alembic.op.execute", side_effect=cur.execute):
        getattr(_load_migration(), method)()
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
class TestRealPostgresRoundTrip:
    """Upgrade / constraint / idempotency / downgrade against genuine Postgres."""

    def test_upgrade_backfills_existing_rows_to_todays_behaviour(
        self, pg_db: PgTestDatabase
    ) -> None:
        _run(pg_db, "upgrade")
        conn = psycopg2.connect(pg_db.dsn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT video_codec, audio_codec FROM streaming_configs WHERE source_url = 'sk_old'"
            )
            assert cur.fetchone() == ("h264", "copy")
            cur.execute(
                "SELECT protocol, video_codec, audio_codec FROM streaming_targets WHERE platform = 'twitch'"
            )
            assert cur.fetchone() == ("rtmp", None, None)
        conn.close()

    def test_upgrade_added_exactly_the_documented_columns(
        self, pg_db: PgTestDatabase
    ) -> None:
        for table, added in _ADDED_COLUMNS.items():
            assert added <= _columns(pg_db, table)

    def test_check_constraints_reject_unknown_vocabulary(
        self, pg_db: PgTestDatabase
    ) -> None:
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
            # The supported vocabulary is accepted.
            cur.execute(
                "UPDATE streaming_configs SET video_codec = 'av1', audio_codec = 'opus'"
            )
            cur.execute(
                "UPDATE streaming_targets SET protocol = 'srt', video_codec = 'h265', audio_codec = 'aac'"
            )
            cur.execute(
                "UPDATE streaming_targets SET video_codec = NULL, audio_codec = NULL"
            )
        conn.close()

    def test_upgrade_is_idempotent(self, pg_db: PgTestDatabase) -> None:
        _run(pg_db, "upgrade")
        _run(pg_db, "upgrade")

    def test_downgrade_removes_the_columns_and_upgrade_restores_them(
        self, pg_db: PgTestDatabase
    ) -> None:
        _run(pg_db, "downgrade")
        for table, added in _ADDED_COLUMNS.items():
            assert not (added & _columns(pg_db, table))
        _run(pg_db, "upgrade")
        for table, added in _ADDED_COLUMNS.items():
            assert added <= _columns(pg_db, table)
