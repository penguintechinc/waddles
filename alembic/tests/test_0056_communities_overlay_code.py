"""Tests for 0056_communities_overlay_code (`communities.overlay_code`).

The column is the unguessable public handle in svc-presentation overlay URLs
(`/{overlay_code}/{surface}`), replacing the enumerable integer community id.
What must hold, and where it is proven:

1. Text-level (no database): revision metadata, a single alembic head, and the
   emitted SQL names the CSPRNG generator (`gen_random_bytes`), the default,
   NOT NULL and the unique constraint.
2. Real Postgres (docker): the actual `upgrade()`/`downgrade()` SQL runs against
   a populated `communities` table. Backfill gives EVERY existing row a code in
   the exact `[0-9a-f]{16}` shape, all DISTINCT; a new row without a code gets
   one from the column DEFAULT; the UNIQUE and CHECK constraints reject
   violations; a half-applied state with duplicate codes is repaired; re-running
   is a no-op; downgrade removes everything it added. None of that can be shown
   against mocked SQL text.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, _free_port, _wait_ready

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent
_VERSIONS = _ALEMBIC_DIR / "versions"
_CODE_RE = re.compile(r"^[0-9a-f]{16}$")
_ROW_COUNT = 40


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration() -> ModuleType:
    return _load(_VERSIONS / "0056_communities_overlay_code.py")


def _captured_sql(fn_name: str, migration: ModuleType) -> str:
    with patch("alembic.op.execute") as mock_execute:
        getattr(migration, fn_name)()
    return "\n".join(str(call.args[0]) for call in mock_execute.call_args_list)


class TestMigrationMetadata:
    def test_revision_ids(self, migration: ModuleType) -> None:
        assert migration.revision == "0056_communities_overlay_code"
        assert migration.down_revision == "0047_builtin_handler_paths"

    def test_revision_id_fits_alembic_version_num_varchar32(
        self, migration: ModuleType
    ) -> None:
        assert len(migration.revision) <= 32

    def test_exactly_one_head_and_it_is_this_revision(self) -> None:
        """heads == 1: no other revision chains off this one's parent, and nothing chains off us."""
        revisions: set[str] = set()
        down_revisions: list[str] = []
        files = [p for p in _VERSIONS.glob("*.py") if p.name != "__init__.py"]
        assert files, "no alembic revisions found: the head check would be vacuous"
        for path in files:
            module = _load(path)
            revisions.add(module.revision)
            if module.down_revision is not None:
                down_revisions.append(module.down_revision)
        heads = revisions - set(down_revisions)
        assert heads == {"0056_communities_overlay_code"}
        assert down_revisions.count("0047_builtin_handler_paths") == 1


class TestEmittedSql:
    def test_upgrade_uses_the_csprng_generator_default_and_constraints(
        self, migration: ModuleType
    ) -> None:
        sql = _captured_sql("upgrade", migration)
        assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in sql
        assert "encode(gen_random_bytes(8), 'hex')" in sql
        assert "SET DEFAULT encode(gen_random_bytes(8), 'hex')" in sql
        assert "SET NOT NULL" in sql
        assert "UNIQUE (overlay_code)" in sql
        assert "[0-9a-f]{16}" in sql
        assert "random()" not in sql, (
            "overlay codes must come from the CSPRNG, never random()"
        )

    def test_downgrade_drops_what_upgrade_added(self, migration: ModuleType) -> None:
        sql = _captured_sql("downgrade", migration)
        assert "DROP COLUMN IF EXISTS overlay_code" in sql
        assert migration.UNIQUE_CONSTRAINT in sql
        assert migration.FORMAT_CONSTRAINT in sql


def _reset_communities(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS communities CASCADE")
        cur.execute(
            "CREATE TABLE communities (id SERIAL PRIMARY KEY, name TEXT, tenant_id INTEGER)"
        )
        cur.execute(
            "INSERT INTO communities (name, tenant_id) "
            "SELECT 'community-' || g, 1 FROM generate_series(1, %s) g",
            (_ROW_COUNT,),
        )


def _run(
    conn: psycopg2.extensions.connection, fn_name: str, migration: ModuleType
) -> None:
    """Run `migration.<fn_name>()` against `conn` with `op.execute` patched to a live cursor."""
    with conn.cursor() as cur, patch("alembic.op.execute", side_effect=cur.execute):
        getattr(migration, fn_name)()


def _codes(conn: psycopg2.extensions.connection) -> dict[int, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, overlay_code FROM communities ORDER BY id")
        return {row_id: code for row_id, code in cur.fetchall()}


def _column_exists(conn: psycopg2.extensions.connection) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'communities' AND column_name = 'overlay_code'"
        )
        return bool(cur.fetchone()[0])


@pytest.fixture(scope="session")
def overlay_code_db() -> Iterator[PgTestDatabase]:
    """One bare Postgres 17 container; each test rebuilds its own `communities` table."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    container = "waddles-migtest-0056-overlay-code"
    port = _free_port()
    db = PgTestDatabase(
        host="127.0.0.1",
        port=port,
        user="waddlebot",
        password="testpass123",
        dbname="waddlebot",
    )
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-e",
            f"POSTGRES_USER={db.user}",
            "-e",
            f"POSTGRES_PASSWORD={db.password}",
            "-e",
            f"POSTGRES_DB={db.dbname}",
            "-p",
            f"{port}:5432",
            "postgres:17-bookworm",
        ],
        capture_output=True,
        check=True,
    )
    try:
        _wait_ready(container, db.user, db.dbname)
        yield db
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )


@pytest.fixture
def conn(overlay_code_db: PgTestDatabase) -> Iterator[psycopg2.extensions.connection]:
    """A fresh autocommit connection with a populated pre-migration `communities` table."""
    connection = psycopg2.connect(overlay_code_db.dsn)
    connection.autocommit = True
    try:
        _reset_communities(connection)
        yield connection
    finally:
        connection.close()


@requires_docker
class TestAgainstRealPostgres:
    def test_backfill_gives_every_existing_community_a_distinct_well_formed_code(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM communities")
            assert (
                cur.fetchone()[0] == _ROW_COUNT
            )  # denominator: the seed really is in place
        assert not _column_exists(conn)

        _run(conn, "upgrade", migration)

        codes = _codes(conn)
        assert len(codes) == _ROW_COUNT
        assert all(_CODE_RE.match(code) for code in codes.values()), codes
        assert len(set(codes.values())) == _ROW_COUNT, (
            "backfilled codes are not distinct"
        )

    def test_codes_are_not_derived_from_the_integer_id(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        _run(conn, "upgrade", migration)
        codes = _codes(conn)
        for community_id, code in codes.items():
            assert int(code, 16) != community_id
            assert code != f"{community_id:016x}"

    def test_a_new_community_gets_a_code_from_the_column_default(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        _run(conn, "upgrade", migration)
        before = set(_codes(conn).values())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO communities (name, tenant_id) VALUES ('late', 1), ('later', 1) "
                "RETURNING overlay_code"
            )
            fresh = [row[0] for row in cur.fetchall()]
        assert len(fresh) == 2
        assert all(_CODE_RE.match(code) for code in fresh), fresh
        assert fresh[0] != fresh[1], (
            "the default must draw per row, not once per statement"
        )
        assert not (set(fresh) & before)

    def test_column_is_not_null_and_unique_and_format_checked(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        _run(conn, "upgrade", migration)
        existing = next(iter(_codes(conn).values()))
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.NotNullViolation):
                cur.execute(
                    "INSERT INTO communities (name, overlay_code) VALUES ('x', NULL)"
                )
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO communities (name, overlay_code) VALUES ('dupe', %s)",
                    (existing,),
                )
            with pytest.raises(psycopg2.errors.StringDataRightTruncation):
                cur.execute(
                    "INSERT INTO communities (name, overlay_code) VALUES ('long', %s)",
                    ("0123456789abcdef0",),
                )
            for malformed in (
                "ABCDEF0123456789",
                "abc",
                "zzzzzzzzzzzzzzzz",
                "0123456789abcde-",
            ):
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO communities (name, overlay_code) VALUES ('bad', %s)",
                        (malformed,),
                    )

    def test_unique_constraint_is_backed_by_an_index(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        _run(conn, "upgrade", migration)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'communities' AND indexname = %s",
                (migration.UNIQUE_CONSTRAINT,),
            )
            row = cur.fetchone()
        assert row is not None, "no index backs the unique constraint"
        assert "UNIQUE" in row[0] and "(overlay_code)" in row[0]

    def test_upgrade_is_idempotent_and_keeps_existing_codes(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        _run(conn, "upgrade", migration)
        first = _codes(conn)
        _run(conn, "upgrade", migration)
        assert _codes(conn) == first

    def test_half_applied_duplicates_are_repaired_keeping_the_lowest_id(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        """A prior partial run left one code on several rows: de-dup keeps the first, re-draws rest."""
        shared = "deadbeefdeadbeef"
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE communities ADD COLUMN overlay_code VARCHAR(16)")
            cur.execute(
                "UPDATE communities SET overlay_code = %s WHERE id IN (3, 4, 5)",
                (shared,),
            )

        _run(conn, "upgrade", migration)

        codes = _codes(conn)
        assert len(set(codes.values())) == _ROW_COUNT
        assert codes[3] == shared, "the lowest-id holder keeps its code"
        assert codes[4] != shared and codes[5] != shared
        assert all(_CODE_RE.match(code) for code in codes.values())

    def test_dedup_fails_loud_instead_of_looping_when_it_cannot_converge(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        """If the generator is broken the bounded loop raises; it never spins forever."""
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            cur.execute("ALTER TABLE communities ADD COLUMN overlay_code VARCHAR(16)")
            cur.execute("UPDATE communities SET overlay_code = 'deadbeefdeadbeef'")
            # A degenerate generator shadowing pgcrypto's (same signature, earlier in the
            # search_path) that always returns the same bytes: re-draws can never clear the
            # duplicates, so the migration must give up with an error.
            cur.execute("CREATE SCHEMA broken_rng")
            cur.execute(
                "CREATE FUNCTION broken_rng.gen_random_bytes(integer) RETURNS bytea AS "
                "$$ SELECT '\\xdeadbeefdeadbeef'::bytea $$ LANGUAGE sql"
            )
            cur.execute("SET search_path = broken_rng, public")
            try:
                with (
                    pytest.raises(
                        psycopg2.errors.RaiseException, match="duplicate codes remain"
                    ),
                    patch("alembic.op.execute", side_effect=cur.execute),
                ):
                    migration.upgrade()
            finally:
                cur.execute("RESET search_path")
                cur.execute("DROP SCHEMA broken_rng CASCADE")

    def test_downgrade_removes_the_column_and_a_reupgrade_mints_fresh_codes(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        _run(conn, "upgrade", migration)
        original = _codes(conn)
        _run(conn, "downgrade", migration)
        assert not _column_exists(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM communities")
            assert cur.fetchone()[0] == _ROW_COUNT, (
                "downgrade must not delete community rows"
            )

        _run(conn, "upgrade", migration)
        reissued = _codes(conn)
        assert len(set(reissued.values())) == _ROW_COUNT
        assert set(reissued.values()) != set(original.values())
