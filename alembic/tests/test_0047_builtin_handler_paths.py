"""Tests for 0047_builtin_handler_paths (rename `bundles` -> `builtin_handlers`).

The migration rewrites `app_catalog.stages.<stage>.entrypoint` strings from
`bundles.<module>:<fn>` to `builtin_handlers.<module>:<fn>` because the directory
`core/svc_*/bundles/` was renamed to `core/svc_*/builtin_handlers/` and that directory
name is the Python import package the stage-runners resolve entrypoints against. Without
the rewrite every built-in app in an existing database fails to load.

Two layers, same split as the sibling migration tests:

1. Text-level (no database): revision metadata, the module allowlist matches reality on
   disk and covers every entrypoint any historical revision ever seeded, and the emitted
   SQL has the right shape.
2. Real Postgres (docker): the real `upgrade()`/`downgrade()` SQL is executed against a
   live `app_catalog` holding old-form, new-form, foreign, look-alike and empty rows -- the
   behavior that matters (only owned entrypoints change, every other JSON key survives,
   re-running is a no-op, downgrade restores) cannot be shown against mocked SQL text.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import psycopg2
import psycopg2.extensions
import pytest
from pg_docker import DOCKER_AVAILABLE, PgTestDatabase, _free_port, _wait_ready

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent
_VERSIONS = _ALEMBIC_DIR / "versions"
_REPO_ROOT = _ALEMBIC_DIR.parent
_HANDLER_DIRS = {
    stage: _REPO_ROOT / "core" / f"svc_{stage}" / "builtin_handlers"
    for stage in ("ingest", "process", "action")
}
_LEGACY_SQL_DIR = _REPO_ROOT / "config" / "postgres" / "migrations"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migration_0047_builtin_handler_paths", _VERSIONS / "0047_builtin_handler_paths.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration() -> ModuleType:
    return _load_migration()


def _captured_sql(fn_name: str, migration: ModuleType) -> list[str]:
    with patch("alembic.op.execute") as mock_execute:
        getattr(migration, fn_name)()
    return [str(call.args[0]) for call in mock_execute.call_args_list]


class TestMigrationMetadata:
    def test_chains_directly_off_0046(self, migration: ModuleType) -> None:
        assert migration.revision == "0047_builtin_handler_paths"
        assert migration.down_revision == "0046_connector_pii_tenant_scope"

    def test_revision_id_fits_alembic_version_num_varchar32(self, migration: ModuleType) -> None:
        assert len(migration.revision) <= 32

    def test_single_head(self) -> None:
        """No other revision may chain off 0046 -- two children of one parent is two heads."""
        down_revisions = []
        for path in _VERSIONS.glob("*.py"):
            if path.name == "__init__.py":
                continue
            spec = importlib.util.spec_from_file_location(path.stem, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            down_revisions.append(module.down_revision)
        assert down_revisions.count("0046_connector_pii_tenant_scope") == 1


class TestModuleAllowlist:
    def test_every_allowlisted_module_ships_under_builtin_handlers(
        self, migration: ModuleType
    ) -> None:
        """The "coded but not routable" guard: a stale name would rewrite to a dead entrypoint."""
        shipped = {p.stem for d in _HANDLER_DIRS.values() for p in d.glob("*.py")}
        missing = sorted(set(migration._MODULES) - shipped)
        assert not missing, f"allowlisted modules not under any builtin_handlers/: {missing}"
        assert len(migration._MODULES) == len(set(migration._MODULES)) == 45

    def test_old_bundles_directories_no_longer_exist(self) -> None:
        """The rename is complete: no `core/svc_*/bundles/` package is left to import."""
        leftovers = [
            f"core/svc_{stage}/bundles"
            for stage in _HANDLER_DIRS
            if (_REPO_ROOT / "core" / f"svc_{stage}" / "bundles").exists()
        ]
        assert not leftovers, f"old package directories still present: {leftovers}"

    def test_every_historically_seeded_entrypoint_module_is_covered(
        self, migration: ModuleType
    ) -> None:
        """Every `bundles.<module>:` any earlier revision/legacy SQL seeded must be rewritten."""
        pattern = re.compile(r"\bbundles\.([a-z][a-z0-9_]*):")
        historical: set[str] = set()
        files_scanned = 0
        sources = [
            p
            for p in sorted(_VERSIONS.glob("*.py")) + sorted(_LEGACY_SQL_DIR.glob("*.sql"))
            if p.name != "0047_builtin_handler_paths.py"
        ]
        for path in sources:
            files_scanned += 1
            historical.update(pattern.findall(path.read_text(encoding="utf-8")))
        # A scanner pointed at the wrong root would report "covered" over zero evidence.
        assert files_scanned > 50
        assert len(historical) >= 20, f"suspiciously few historical entrypoints: {historical}"
        uncovered = sorted(historical - set(migration._MODULES))
        assert not uncovered, f"historical entrypoint modules the rewrite would miss: {uncovered}"


class TestEmittedSql:
    def test_upgrade_emits_one_update_per_stage_targeting_builtin_handlers(
        self, migration: ModuleType
    ) -> None:
        statements = _captured_sql("upgrade", migration)
        assert len(statements) == 3
        for stage, sql in zip(("ingest", "process", "action"), statements, strict=True):
            assert sql.count("UPDATE app_catalog") == 1
            assert "{" + f"{stage},entrypoint" + "}" in sql
            assert "'builtin_handlers.'" in sql
            assert r"'^bundles\.(" in sql

    def test_downgrade_is_the_exact_inverse_direction(self, migration: ModuleType) -> None:
        statements = _captured_sql("downgrade", migration)
        assert len(statements) == 3
        for sql in statements:
            assert "'bundles.'" in sql
            assert r"'^builtin_handlers\.(" in sql

    def test_never_destructive(self, migration: ModuleType) -> None:
        for fn in ("upgrade", "downgrade"):
            sql = "\n".join(_captured_sql(fn, migration)).upper()
            assert "DELETE" not in sql and "DROP" not in sql and "TRUNCATE" not in sql

    def test_unknown_stage_fails_loud(self, migration: ModuleType) -> None:
        with pytest.raises(ValueError, match="unknown stage"):
            migration._rewrite_sql("presentation", "bundles", "builtin_handlers")


_TABLE_SQL = """
CREATE TABLE app_catalog (
    app_id VARCHAR(255) PRIMARY KEY,
    stages JSONB DEFAULT '{}'::jsonb
);
"""

#: app_id -> stages JSON before the migration.
_ROWS: dict[str, dict | None] = {
    # Fully populated built-in app: ingest + process + action, with sibling keys that
    # must survive byte-for-byte.
    "waddles.bot.twitch.default": {
        "ingest": {
            "entrypoint": "bundles.twitch_ingest:normalize",
            "consumes": ["twitch.message"],
            "config": {},
            "spec": {"required_config": ["channel"]},
        },
        "process": {"entrypoint": "bundles.bot_process:transform", "config": {}, "spec": {}},
        "action": {
            "entrypoint": "bundles.twitch_send_action:send_message",
            "config": {"api_base": "https://example.invalid"},
            "spec": {"required_config": ["bot_token_ref"]},
        },
    },
    # A module this migration does not own (e.g. a WASI bundle's own `bundles` package).
    "waddles.core.example.foreign": {
        "process": {"entrypoint": "bundles.not_a_builtin_module:transform"},
    },
    # A look-alike that merely starts with an owned name: the trailing `:` must pin it.
    "waddles.core.example.lookalike": {
        "process": {"entrypoint": "bundles.bot_process_v2:transform"},
    },
    # Already in the new form (e.g. seeded by a post-rename migration): must be a no-op.
    "waddles.bot.discord.already_new": {
        "ingest": {"entrypoint": "builtin_handlers.discord_ingest:normalize"},
    },
    # Presentation-style stage with no script entrypoint, JSON-null entrypoint, empty and NULL.
    "waddles.core.example.presentation": {"presentation": {"html_entrypoint": "overlay.html"}},
    "waddles.core.example.null_entry": {"process": {"entrypoint": None}},
    "waddles.core.example.empty": {},
    "waddles.core.example.null_stages": None,
}

_EXPECTED_AFTER_UPGRADE = {
    **_ROWS,
    "waddles.bot.twitch.default": {
        "ingest": {
            "entrypoint": "builtin_handlers.twitch_ingest:normalize",
            "consumes": ["twitch.message"],
            "config": {},
            "spec": {"required_config": ["channel"]},
        },
        "process": {
            "entrypoint": "builtin_handlers.bot_process:transform",
            "config": {},
            "spec": {},
        },
        "action": {
            "entrypoint": "builtin_handlers.twitch_send_action:send_message",
            "config": {"api_base": "https://example.invalid"},
            "spec": {"required_config": ["bot_token_ref"]},
        },
    },
}


def _run(conn: psycopg2.extensions.connection, fn_name: str, migration: ModuleType) -> None:
    """Run `migration.<fn_name>()` against `conn` with `op.execute` patched to a live cursor."""
    with conn.cursor() as cur, patch("alembic.op.execute", side_effect=cur.execute):
        getattr(migration, fn_name)()


def _snapshot(conn: psycopg2.extensions.connection) -> dict[str, dict | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT app_id, stages FROM app_catalog ORDER BY app_id")
        return {app_id: stages for app_id, stages in cur.fetchall()}


@pytest.fixture(scope="session")
def pg_handler_paths_db() -> Iterator[PgTestDatabase]:
    """One bare Postgres 17 container holding just the `app_catalog` shape 0047 touches."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    container = "waddles-migtest-0047-handler-paths"
    port = _free_port()
    db = PgTestDatabase(
        host="127.0.0.1", port=port, user="waddlebot", password="testpass123", dbname="waddlebot"
    )
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    subprocess.run(
        [
            "docker", "run", "-d", "--name", container,
            "-e", f"POSTGRES_USER={db.user}",
            "-e", f"POSTGRES_PASSWORD={db.password}",
            "-e", f"POSTGRES_DB={db.dbname}",
            "-p", f"{port}:5432",
            "postgres:17-bookworm",
        ],
        capture_output=True,
        check=True,
    )
    try:
        _wait_ready(container, db.user, db.dbname)
        subprocess.run(
            ["docker", "exec", "-i", container, "psql", "-U", db.user, "-d", db.dbname],
            input=_TABLE_SQL,
            text=True,
            capture_output=True,
            check=True,
        )
        yield db
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


@pytest.fixture
def conn(pg_handler_paths_db: PgTestDatabase) -> Iterator[psycopg2.extensions.connection]:
    """A fresh autocommit connection with `app_catalog` reset to the pre-migration rows."""
    connection = psycopg2.connect(pg_handler_paths_db.dsn)
    connection.autocommit = True
    try:
        with connection.cursor() as cur:
            cur.execute("TRUNCATE app_catalog")
            for app_id, stages in _ROWS.items():
                cur.execute(
                    "INSERT INTO app_catalog (app_id, stages) VALUES (%s, %s::jsonb)",
                    (app_id, None if stages is None else json.dumps(stages)),
                )
        yield connection
    finally:
        connection.close()


@requires_docker
class TestAgainstRealPostgres:
    def test_upgrade_rewrites_only_owned_entrypoints_and_preserves_every_other_key(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        assert _snapshot(conn) == _ROWS  # denominator: the seed really is in place
        _run(conn, "upgrade", migration)
        assert _snapshot(conn) == _EXPECTED_AFTER_UPGRADE

    def test_upgrade_is_idempotent(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        _run(conn, "upgrade", migration)
        first = _snapshot(conn)
        _run(conn, "upgrade", migration)
        assert _snapshot(conn) == first == _EXPECTED_AFTER_UPGRADE

    def test_downgrade_restores_the_original_rows(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        _run(conn, "upgrade", migration)
        _run(conn, "downgrade", migration)
        restored = _snapshot(conn)
        # The already-new row is legitimately converted back by downgrade (it is an owned
        # module in the new form); every other row must round-trip exactly.
        expected = {
            **_ROWS,
            "waddles.bot.discord.already_new": {
                "ingest": {"entrypoint": "bundles.discord_ingest:normalize"}
            },
        }
        assert restored == expected

    def test_every_owned_module_is_rewritten_in_every_stage(
        self, conn: psycopg2.extensions.connection, migration: ModuleType
    ) -> None:
        """No module in the allowlist is skipped by the regex alternation or the stage loop."""
        with conn.cursor() as cur:
            cur.execute("TRUNCATE app_catalog")
            for index, module in enumerate(migration._MODULES):
                stages = {
                    stage: {"entrypoint": f"bundles.{module}:fn"}
                    for stage in ("ingest", "process", "action")
                }
                cur.execute(
                    "INSERT INTO app_catalog (app_id, stages) VALUES (%s, %s::jsonb)",
                    (f"waddles.core.example.m{index}", json.dumps(stages)),
                )
        rows_before = _snapshot(conn)
        assert len(rows_before) == len(migration._MODULES)
        _run(conn, "upgrade", migration)
        rewritten = 0
        for index, module in enumerate(migration._MODULES):
            stages = _snapshot(conn)[f"waddles.core.example.m{index}"]
            assert stages is not None
            for stage in ("ingest", "process", "action"):
                assert stages[stage]["entrypoint"] == f"builtin_handlers.{module}:fn"
                rewritten += 1
        assert rewritten == 3 * len(migration._MODULES)
