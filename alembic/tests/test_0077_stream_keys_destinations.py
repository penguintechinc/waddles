"""Real-Postgres tests for 0077_stream_keys_destinations.

A fresh DB goes through the whole chain (baseline replay creates `streaming_configs`, 0076 adds the
key-hash columns + `streaming_destinations`); the minimal CI bootstrap has no streaming tables so the
DDL is skipped; downgrade removes everything 0076 added. Constraint checks pin the key-hash UNIQUE
index and the h264-only / platform vocabulary of `streaming_destinations`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import closing

import psycopg2
import pytest
from pg_docker import (
    DOCKER_AVAILABLE,
    PgTestDatabase,
    alembic_cli,
    empty_postgres,
    migrated_postgres,
)

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker CLI not available")

_NEW_COLUMNS = {"stream_key_hash", "stream_key_fingerprint", "stream_key_rotated_at"}


def _q(dsn: str, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
    with closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [tuple(r) for r in cur.fetchall()] if cur.description else []


def _config_columns(dsn: str) -> set[str]:
    rows = _q(
        dsn,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='streaming_configs'",
    )
    return {str(r[0]) for r in rows}


def _exists(dsn: str, table: str) -> bool:
    return bool(
        _q(dsn, "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))[0][0]
    )


@pytest.fixture(scope="module")
def fresh_db() -> Iterator[PgTestDatabase]:
    with empty_postgres("0077-fresh") as db:
        alembic_cli("upgrade", "head", dsn=db.dsn)
        yield db


def test_revision_id_within_limit() -> None:
    assert len("0077_stream_keys_destinations") <= 32


def test_fresh_db_has_columns_and_table(fresh_db: PgTestDatabase) -> None:
    assert _NEW_COLUMNS <= _config_columns(fresh_db.dsn)
    assert _exists(fresh_db.dsn, "streaming_destinations")


def test_key_hash_is_unique_and_nulls_allowed(fresh_db: PgTestDatabase) -> None:
    tenant = _q(fresh_db.dsn, "SELECT id FROM tenants ORDER BY id LIMIT 1")[0][0]
    ids = []
    for n in range(3):
        ids.append(
            _q(
                fresh_db.dsn,
                "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
                (f"c0077-{n}", tenant),
            )[0][0]
        )
    for cid, digest in ((ids[0], "a" * 64), (ids[2], None)):
        _q(
            fresh_db.dsn,
            "INSERT INTO streaming_configs (community_id, source_url, stream_key_hash) "
            "VALUES (%s, '', %s)",
            (cid, digest),
        )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _q(
            fresh_db.dsn,
            "INSERT INTO streaming_configs (community_id, source_url, stream_key_hash) "
            "VALUES (%s, '', %s)",
            (ids[1], "a" * 64),
        )


def test_destination_constraints(fresh_db: PgTestDatabase) -> None:
    cfg = _q(fresh_db.dsn, "SELECT id FROM streaming_configs ORDER BY id LIMIT 1")[0][0]
    base = (
        "INSERT INTO streaming_destinations (public_id, config_id, platform, display_url, "
        "rtmp_url_enc, stream_key_enc, {col}) VALUES (%s, %s, %s, 'rtmp://h', 'x', 'y', %s)"
    )
    _q(fresh_db.dsn, base.format(col="video_codec"), ("p1", cfg, "twitch", "h264"))
    for col, platform, value in (
        ("video_codec", "twitch", "h265"),
        ("video_codec", "twitch", "av1"),
        ("max_resolution", "twitch", "8k"),
        ("video_codec", "myspace", "h264"),
    ):
        with pytest.raises(psycopg2.errors.CheckViolation):
            _q(
                fresh_db.dsn,
                base.format(col=col),
                (f"p-{col}-{value}", cfg, platform, value),
            )


def test_downgrade_removes_everything_and_upgrade_restores() -> None:
    with empty_postgres("0077-down") as db:
        alembic_cli("upgrade", "head", dsn=db.dsn)
        alembic_cli("downgrade", "0076_suggestion_community_id", dsn=db.dsn)
        assert not _exists(db.dsn, "streaming_destinations")
        assert _NEW_COLUMNS <= _config_columns(db.dsn)  # retained on purpose
        alembic_cli("upgrade", "head", dsn=db.dsn)
        assert _NEW_COLUMNS <= _config_columns(db.dsn)


def test_minimal_bootstrap_skips_without_error() -> None:
    with migrated_postgres("0077-min") as db:
        assert not _exists(db.dsn, "streaming_configs")
        assert not _exists(db.dsn, "streaming_destinations")
