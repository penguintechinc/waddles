"""Real-Postgres tests for 0061_port_legacy_sql_103_codec (legacy SQL 103 -> Alembic).

# regression: config/postgres SQL migration 103 never reached existing DBs (streaming codec columns)

`0001_baseline` replays `config/postgres/migrations/*.sql` only on a fresh database, so
`103_streaming_codec_columns.sql` never ran on an existing DB (alpha/beta/prod) and
`streaming_configs.video_codec` / `streaming_targets.protocol` were missing. 0061 is the
Alembic bridge. Real-Postgres scenarios (no mocked `op.execute`):

  - `fresh_db`: empty DB, `alembic upgrade head` -- the baseline replay already applied 103,
    so 0061 must be a clean no-op (and still ledger it);
  - `existing_db`: a DB whose legacy replay stops at 102 (seeded with a streaming config +
    target), THEN `alembic upgrade head`; 0061 must add the five columns with the
    pre-103-equivalent defaults on the existing rows and ledger 103;
  - the minimal CI bootstrap (`migrated_postgres`): no streaming tables at all, so the DDL is
    skipped and no ledger is invented.

Plus: the two paths end in the same schema, CHECK constraints pin the vocabulary, downgrade
drops the columns + ledger row, and re-upgrading restores them.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest import mock

import psycopg2
import pytest
from pg_docker import (
    DOCKER_AVAILABLE,
    PgTestDatabase,
    alembic_cli,
    empty_postgres,
    migrated_postgres,
)

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SQL_DIR = _REPO_ROOT / "config" / "postgres" / "migrations"
_REVISION_PATH = _REPO_ROOT / "alembic" / "versions" / "0061_port_legacy_sql_103_codec.py"

#: First numbered SQL file an "existing DB" has NOT seen: the legacy replay stops at 102.
_FIRST_UNSEEN_SQL = 103

#: `(table, column)` pairs 103 adds.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("streaming_configs", "video_codec"),
    ("streaming_configs", "audio_codec"),
    ("streaming_targets", "protocol"),
    ("streaming_targets", "video_codec"),
    ("streaming_targets", "audio_codec"),
)

Rows = list[tuple[object, ...]]


def _load_revision() -> ModuleType:
    """Import the 0061 revision module by path (it is not an importable package)."""
    spec = importlib.util.spec_from_file_location("waddles_alembic_0061", _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REVISION = _load_revision()
_PRE_REVISION: str = _REVISION.down_revision
_LEGACY_VERSIONS: tuple[str, ...] = _REVISION.LEGACY_VERSIONS


def _query(dsn: str, sql: str, params: Sequence[object] = ()) -> Rows:
    """Run one statement (autocommit) on a short-lived connection and return every row."""
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [tuple(row) for row in cur.fetchall()] if cur.description else []


def _execute(dsn: str, sql: str, params: Sequence[object] = ()) -> None:
    """Run one write statement on a short-lived autocommitting connection."""
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _scalar(dsn: str, sql: str, params: Sequence[object] = ()) -> object:
    """Return the first column of the first row of a read query."""
    rows = _query(dsn, sql, params)
    assert rows, f"no rows for: {sql}"
    return rows[0][0]


def _exists(dsn: str, table: str) -> bool:
    """True when `public.<table>` exists."""
    return bool(_scalar(dsn, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)))


def _ledger(dsn: str) -> set[str]:
    """Every `schema_migrations.version` currently recorded."""
    return {str(row[0]) for row in _query(dsn, "SELECT version FROM schema_migrations")}


def _present_columns(dsn: str) -> set[tuple[str, str]]:
    """Which of the five 103 `(table, column)` pairs exist right now."""
    rows = _query(
        dsn,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name IN ('streaming_configs', 'streaming_targets')",
    )
    return {(str(t), str(c)) for t, c in rows} & set(_COLUMNS)


def _snapshot(dsn: str) -> dict[str, object]:
    """Columns + CHECK constraints of the two streaming tables, order-independent."""
    snap: dict[str, object] = {}
    for table in ("streaming_configs", "streaming_targets"):
        snap[f"{table}.columns"] = _query(
            dsn,
            "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY column_name",
            (table,),
        )
        snap[f"{table}.checks"] = _query(
            dsn,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND contype = 'c' ORDER BY 1",
            (f"public.{table}",),
        )
    return snap


@dataclass(slots=True, frozen=True)
class SeededDb:
    """An existing-DB simulation after `upgrade head`, plus the ids seeded beforehand."""

    db: PgTestDatabase
    config_id: int
    target_id: int


def _legacy_sql_before(cutoff: int, dest: Path) -> int:
    """Copy `*.sql` files numbered < `cutoff` into `dest`; return how many."""
    copied = 0
    for sql_file in sorted(_SQL_DIR.glob("*.sql")):
        if int(sql_file.name[:3]) < cutoff:
            shutil.copy(sql_file, dest / sql_file.name)
            copied += 1
    return copied


def _seed_pre_103(dsn: str) -> tuple[int, int]:
    """Seed a streaming config + target in their pre-103 shape; return (config_id, target_id)."""
    tenant = int(str(_scalar(dsn, "SELECT id FROM tenants ORDER BY id LIMIT 1")))
    community = int(
        str(
            _scalar(
                dsn,
                "INSERT INTO communities (name, tenant_id) VALUES ('port-0061', %s) RETURNING id",
                (tenant,),
            )
        )
    )
    config = int(
        str(
            _scalar(
                dsn,
                "INSERT INTO streaming_configs (community_id, source_url) "
                "VALUES (%s, 'rtmp://example.invalid/live') RETURNING id",
                (community,),
            )
        )
    )
    target = int(
        str(
            _scalar(
                dsn,
                "INSERT INTO streaming_targets (config_id, platform, forward_url) "
                "VALUES (%s, 'twitch', 'rtmp://example.invalid/forward') RETURNING id",
                (config,),
            )
        )
    )
    return config, target


@pytest.fixture(scope="module")
def fresh_db() -> Iterator[PgTestDatabase]:
    """An empty DB taken through the whole chain: baseline replay applies 103 first."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with empty_postgres("0061-fresh") as db:
        alembic_cli("upgrade", "head", dsn=db.dsn)
        yield db


@pytest.fixture(scope="module")
def existing_db() -> Iterator[SeededDb]:
    """A DB baselined before 103 (legacy replay <= 102), seeded, then upgraded to head."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    sql_dir = Path(tempfile.mkdtemp(prefix="waddles-0061-legacy-sql-"))
    try:
        assert _legacy_sql_before(_FIRST_UNSEEN_SQL, sql_dir) > 0, "no legacy SQL copied"
        with empty_postgres("0061-existing") as db:
            with mock.patch.dict(os.environ, {"LEGACY_SQL_DIR": str(sql_dir)}):
                alembic_cli("upgrade", _PRE_REVISION, dsn=db.dsn)

            # Pre-state: this really is a DB the port has to fix.
            assert _exists(db.dsn, "streaming_configs") and _exists(db.dsn, "streaming_targets")
            assert _present_columns(db.dsn) == set(), "103 columns must not exist before 0061"
            assert set(_LEGACY_VERSIONS) & _ledger(db.dsn) == set(), "103 must be unledgered"

            config, target = _seed_pre_103(db.dsn)
            alembic_cli("upgrade", "head", dsn=db.dsn)
            yield SeededDb(db, config, target)
    finally:
        shutil.rmtree(sql_dir, ignore_errors=True)


def test_ledger_version_names_a_real_sql_file() -> None:
    """The ledger version 0061 writes is the SQL file on disk (denominator = 1, never 0)."""
    assert _LEGACY_VERSIONS == ("103_streaming_codec_columns",)
    assert (_SQL_DIR / f"{_LEGACY_VERSIONS[0]}.sql").is_file()
    assert _REVISION.down_revision == "0060_port_legacy_sql_097_102"
    assert len(_REVISION.revision) <= 32


@requires_docker
def test_fresh_replay_ends_with_columns_and_ledger(fresh_db: PgTestDatabase) -> None:
    """Fresh path: baseline applied 103, 0061 no-ops cleanly and the ledger row exists."""
    assert _present_columns(fresh_db.dsn) == set(_COLUMNS)
    assert set(_LEGACY_VERSIONS) <= _ledger(fresh_db.dsn)


@requires_docker
def test_existing_db_gets_the_five_columns_and_ledger(existing_db: SeededDb) -> None:
    """Existing path: 0061 adds all five columns and records 103 in the ledger."""
    dsn = existing_db.db.dsn
    assert _present_columns(dsn) == set(_COLUMNS)
    assert set(_LEGACY_VERSIONS) <= _ledger(dsn)


@requires_docker
def test_existing_rows_keep_pre_103_behaviour(existing_db: SeededDb) -> None:
    """Rows seeded before 0061 read back H.264 / copy / RTMP, overrides NULL (inherit)."""
    dsn = existing_db.db.dsn
    assert _query(
        dsn,
        "SELECT video_codec, audio_codec FROM streaming_configs WHERE id = %s",
        (existing_db.config_id,),
    ) == [("h264", "copy")]
    assert _query(
        dsn,
        "SELECT protocol, video_codec, audio_codec FROM streaming_targets WHERE id = %s",
        (existing_db.target_id,),
    ) == [("rtmp", None, None)]


@requires_docker
def test_check_constraints_pin_the_codec_and_protocol_vocabulary(existing_db: SeededDb) -> None:
    """Valid values are accepted; an out-of-vocabulary value is rejected by Postgres."""
    dsn = existing_db.db.dsn
    _execute(
        dsn,
        "UPDATE streaming_configs SET video_codec = 'av1', audio_codec = 'opus' WHERE id = %s",
        (existing_db.config_id,),
    )
    _execute(
        dsn,
        "UPDATE streaming_targets SET protocol = 'srt', video_codec = 'h265', audio_codec = 'aac' "
        "WHERE id = %s",
        (existing_db.target_id,),
    )
    for sql in (
        "UPDATE streaming_configs SET video_codec = 'vp9' WHERE id = %s",
        "UPDATE streaming_configs SET audio_codec = 'flac' WHERE id = %s",
    ):
        with pytest.raises(psycopg2.errors.CheckViolation):
            _execute(dsn, sql, (existing_db.config_id,))
    for sql in (
        "UPDATE streaming_targets SET protocol = 'whip' WHERE id = %s",
        "UPDATE streaming_targets SET video_codec = 'vp9' WHERE id = %s",
        "UPDATE streaming_targets SET audio_codec = 'flac' WHERE id = %s",
    ):
        with pytest.raises(psycopg2.errors.CheckViolation):
            _execute(dsn, sql, (existing_db.target_id,))
    # Restore the defaults so later tests see the seeded shape.
    _execute(
        dsn,
        "UPDATE streaming_configs SET video_codec = 'h264', audio_codec = 'copy' WHERE id = %s",
        (existing_db.config_id,),
    )
    _execute(
        dsn,
        "UPDATE streaming_targets SET protocol = 'rtmp', video_codec = NULL, audio_codec = NULL "
        "WHERE id = %s",
        (existing_db.target_id,),
    )


@requires_docker
def test_existing_and_fresh_paths_converge_on_one_schema(
    fresh_db: PgTestDatabase, existing_db: SeededDb
) -> None:
    """Columns and CHECK constraints match between the fresh and the existing path."""
    fresh = _snapshot(fresh_db.dsn)
    existing = _snapshot(existing_db.db.dsn)
    assert fresh.keys() == existing.keys() and len(fresh) == 4
    drift = {key: (fresh[key], existing[key]) for key in fresh if fresh[key] != existing[key]}
    assert drift == {}


@requires_docker
@pytest.mark.parametrize("which", ["fresh", "existing"])
def test_downgrade_drops_columns_and_ledger_then_upgrade_restores(
    which: str, fresh_db: PgTestDatabase, existing_db: SeededDb
) -> None:
    """Downgrade removes the five columns + ledger row (rows intact); re-upgrade restores them."""
    dsn = fresh_db.dsn if which == "fresh" else existing_db.db.dsn
    schema_before = _snapshot(dsn)
    configs_before = _scalar(dsn, "SELECT count(*) FROM streaming_configs")
    targets_before = _scalar(dsn, "SELECT count(*) FROM streaming_targets")

    alembic_cli("downgrade", _PRE_REVISION, dsn=dsn)
    assert _present_columns(dsn) == set(), "downgrade must drop all five 103 columns"
    assert not (set(_LEGACY_VERSIONS) & _ledger(dsn)), "downgrade must remove the ledger row"
    assert _scalar(dsn, "SELECT count(*) FROM streaming_configs") == configs_before
    assert _scalar(dsn, "SELECT count(*) FROM streaming_targets") == targets_before

    alembic_cli("upgrade", "head", dsn=dsn)
    assert _present_columns(dsn) == set(_COLUMNS)
    assert set(_LEGACY_VERSIONS) <= _ledger(dsn)
    assert _snapshot(dsn) == schema_before


@requires_docker
def test_minimal_ci_bootstrap_skips_ddl_without_inventing_a_ledger() -> None:
    """No legacy baseline (the CI/harness stamp): no streaming tables, 0061 skips, no ledger faked."""
    with migrated_postgres("0061-harness") as db:
        assert not _exists(db.dsn, "streaming_configs"), "0061 must not fabricate legacy tables"
        assert not _exists(db.dsn, "streaming_targets"), "0061 must not fabricate legacy tables"
        assert not _exists(db.dsn, "schema_migrations"), "0061 must not invent a legacy ledger"
        # The PR job's downgrade/upgrade round trip must also survive without the tables.
        alembic_cli("downgrade", _PRE_REVISION, dsn=db.dsn)
        alembic_cli("upgrade", "head", dsn=db.dsn)
        assert not _exists(db.dsn, "streaming_configs")
