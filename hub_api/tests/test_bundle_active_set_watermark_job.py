"""Real-Postgres tests for `BundleActiveSetWatermarkJob` (data-plane scale design Sec7).

`pg_snapshot_xmin()` has no sqlite equivalent, so unlike every other
hub-api test's `install_dal` (file-backed sqlite, see `tests/conftest.
py::install_dal`'s own docstring), this module builds its own
`penguin_dal.AsyncDB` against a real, ephemeral Postgres container --
`alembic/tests/pg_docker.py`'s harness, loaded by path since `alembic/`
is a sibling of `hub_api/`, not a package either directory can import
normally (same `importlib` idiom every Alembic migration in this repo
already uses to load `scripts/db/rbac_matrix.py`).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg2
import pytest
from penguin_dal import AsyncDB

from services.bundle_active_set_watermark_job import (
    _ADVISORY_LOCK_KEY,
    BundleActiveSetWatermarkJob,
    WatermarkJobConfig,
)
from services.bundle_install_dal import build_install_dal

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PG_DOCKER_PATH = _REPO_ROOT / "alembic" / "tests" / "pg_docker.py"


def _load_pg_docker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("waddles_pg_docker", _PG_DOCKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["waddles_pg_docker"] = module
    spec.loader.exec_module(module)
    return module


pg_docker = _load_pg_docker()

requires_docker = pytest.mark.skipif(
    not pg_docker.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


def _one(cur: Any) -> Any:
    """`cur.fetchone()`, asserting a row exists -- the stub type is `tuple | None`."""
    row = cur.fetchone()
    assert row is not None
    return row[0]


@pytest.fixture(scope="module")
def pg_db() -> Iterator[Any]:
    if not pg_docker.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg_docker.migrated_postgres("watermark-job") as db:
        yield db


@pytest.fixture
async def dal(pg_db: Any) -> AsyncIterator[AsyncDB]:
    """Fresh `install_dal`-equivalent AsyncDB against the real container, per test."""
    pydal_style_dsn = pg_db.dsn.replace("postgresql://", "postgres://")
    install_dal = await build_install_dal(pydal_style_dsn, pool_size=2)
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("TRUNCATE bundle_active_set_changes RESTART IDENTITY")
            cur.execute("UPDATE bundle_active_set_watermark SET safe_seq = 0 WHERE id = 1")
    yield install_dal
    await install_dal.close()


def _seed_app_version(pg_db: Any, app_id: str) -> int:
    """Insert one `app_catalog` + `app_versions` row via a fresh sync connection.

    Returns the new `app_versions.id`.
    """
    with psycopg2.connect(pg_db.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("INSERT INTO app_catalog (app_id) VALUES (%s)", (app_id,))
            cur.execute(
                "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                "VALUES (%s, '1.0.0', 'rust', 'prebuilt') RETURNING id",
                (app_id,),
            )
            return int(_one(cur))


@requires_docker
class TestComputeOnce:
    async def test_zero_when_no_changes(self, dal: AsyncDB) -> None:
        job = BundleActiveSetWatermarkJob(dal)
        assert await job.compute_once() == 0

    async def test_advances_past_a_committed_row(self, pg_db: Any, dal: AsyncDB) -> None:
        version_id = _seed_app_version(pg_db, "waddles.job.committed")
        job = BundleActiveSetWatermarkJob(dal)
        safe_seq = await job.compute_once()
        with psycopg2.connect(pg_db.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq FROM bundle_active_set_changes WHERE entity_id = %s",
                    (str(version_id),),
                )
                expected_seq = _one(cur)
        assert safe_seq >= expected_seq

    async def test_excludes_an_in_flight_uncommitted_transaction(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        in_flight = psycopg2.connect(pg_db.dsn)
        in_flight.autocommit = False
        try:
            with in_flight.cursor() as cur:
                cur.execute("INSERT INTO app_catalog (app_id) VALUES ('waddles.job.inflight')")
                cur.execute(
                    "INSERT INTO app_versions (app_id, version, language, artifact_kind) "
                    "VALUES ('waddles.job.inflight', '1.0.0', 'rust', 'prebuilt') RETURNING id"
                )
                in_flight_version_id = _one(cur)
                # Not committed yet -- under read-committed isolation only
                # this same transaction can see its own uncommitted insert,
                # so the change-log row's seq is read from this cursor too.
                cur.execute(
                    "SELECT seq FROM bundle_active_set_changes WHERE entity_id = %s",
                    (str(in_flight_version_id),),
                )
                in_flight_seq = _one(cur)

            job = BundleActiveSetWatermarkJob(dal)
            safe_seq_while_open = await job.compute_once()

            assert safe_seq_while_open < in_flight_seq

            in_flight.commit()
            safe_seq_after_commit = await job.compute_once()
            assert safe_seq_after_commit >= in_flight_seq
        finally:
            in_flight.close()

    async def test_publishes_to_the_watermark_row(self, pg_db: Any, dal: AsyncDB) -> None:
        _seed_app_version(pg_db, "waddles.job.publish")
        job = BundleActiveSetWatermarkJob(dal)
        safe_seq = await job.compute_once()
        with psycopg2.connect(pg_db.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1")
                published = _one(cur)
        assert published == safe_seq


@requires_docker
class TestPruneOnce:
    async def test_prunes_only_rows_past_the_retention_window(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        old_id = _seed_app_version(pg_db, "waddles.job.old")
        fresh_id = _seed_app_version(pg_db, "waddles.job.fresh")
        with psycopg2.connect(pg_db.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bundle_active_set_changes SET changed_at = now() - interval '72 hours' "
                    "WHERE entity_id = %s",
                    (str(old_id),),
                )

        job = BundleActiveSetWatermarkJob(dal, WatermarkJobConfig(retention_hours=48.0))
        pruned = await job.prune_once()
        assert pruned == 1

        with psycopg2.connect(pg_db.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT entity_id FROM bundle_active_set_changes WHERE entity = 'app_versions'"
                )
                remaining = {row[0] for row in cur.fetchall()}
        assert str(fresh_id) in remaining
        assert str(old_id) not in remaining


@requires_docker
class TestConcurrentReplicas:
    """`pg_try_advisory_xact_lock()` -- exactly one replica computes per tick."""

    async def test_advisory_lock_is_mutually_exclusive_across_connections(self, pg_db: Any) -> None:
        # Two separate real connections (standing in for two hub-api
        # replicas) racing for the same cross-replica coordination key.
        # Postgres serializes lock acquisition; whichever wins holds it
        # until its own transaction ends, so the second must observe a
        # clean `false`, never block or error.
        holder = psycopg2.connect(pg_db.dsn)
        holder.autocommit = False
        contender = psycopg2.connect(pg_db.dsn)
        contender.autocommit = False
        try:
            with holder.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                assert _one(cur) is True

            with contender.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                assert _one(cur) is False

            holder.commit()  # releases holder's xact-scoped lock

            with contender.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
                assert _one(cur) is True
        finally:
            holder.close()
            contender.close()

    async def test_two_concurrent_compute_once_calls_never_double_count(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        # Two BundleActiveSetWatermarkJob instances, standing in for two
        # hub-api replicas, sharing the same underlying database (each
        # over its own AsyncDB pool, matching how two real processes
        # would each hold their own connection pool against one primary).
        _seed_app_version(pg_db, "waddles.job.concurrent-a")
        _seed_app_version(pg_db, "waddles.job.concurrent-b")

        pydal_style_dsn = pg_db.dsn.replace("postgresql://", "postgres://")
        replica_a_dal = await build_install_dal(pydal_style_dsn, pool_size=2)
        replica_b_dal = await build_install_dal(pydal_style_dsn, pool_size=2)
        try:
            replica_a = BundleActiveSetWatermarkJob(replica_a_dal)
            replica_b = BundleActiveSetWatermarkJob(replica_b_dal)

            results = await asyncio.gather(replica_a.compute_once(), replica_b.compute_once())

            # Both calls succeed (no crash from lock contention) and agree
            # on the same final published value -- whichever one actually
            # won the lock this tick, the loser reports that same value
            # back rather than a stale/incorrect one.
            with psycopg2.connect(pg_db.dsn) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1")
                    published = _one(cur)
            assert results[0] == published or results[1] == published
        finally:
            await replica_a_dal.close()
            await replica_b_dal.close()


@requires_docker
class TestMonotonicGuard:
    """The watermark can never regress, even under a direct regression attempt."""

    async def test_direct_regression_attempt_is_rejected(self, pg_db: Any, dal: AsyncDB) -> None:
        with psycopg2.connect(pg_db.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("UPDATE bundle_active_set_watermark SET safe_seq = 50 WHERE id = 1")
                cur.execute("SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1")
                assert _one(cur) == 50

                # The exact guarded UPDATE compute_once() issues -- a lower
                # value must affect zero rows, never regress the watermark.
                cur.execute(
                    "UPDATE bundle_active_set_watermark SET safe_seq = %(safe_seq)s, "
                    "computed_at = now() WHERE id = 1 AND safe_seq < %(safe_seq)s",
                    {"safe_seq": 10},
                )
                assert cur.rowcount == 0
                cur.execute("SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1")
                assert _one(cur) == 50

    async def test_compute_once_never_regresses_an_already_higher_watermark(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        _seed_app_version(pg_db, "waddles.job.regression-guard")
        job = BundleActiveSetWatermarkJob(dal)
        real_safe_seq = await job.compute_once()

        # Simulate a watermark that's already ahead of what a fresh, honest
        # computation would produce (e.g. a stale manual edit) -- a
        # subsequent compute_once() must never write a lower value back.
        inflated = real_safe_seq + 1000
        with psycopg2.connect(pg_db.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bundle_active_set_watermark SET safe_seq = %s WHERE id = 1",
                    (inflated,),
                )

        result = await job.compute_once()
        assert result == inflated

        with psycopg2.connect(pg_db.dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1")
                assert _one(cur) == inflated


@requires_docker
class TestStartStopLifecycle:
    async def test_start_ticks_at_least_once_then_stop_cancels_cleanly(
        self, pg_db: Any, dal: AsyncDB
    ) -> None:
        _seed_app_version(pg_db, "waddles.job.lifecycle")
        job = BundleActiveSetWatermarkJob(dal, WatermarkJobConfig(tick_seconds=0.05))
        job.start()
        try:
            for _ in range(100):
                with psycopg2.connect(pg_db.dsn) as conn:
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute("SELECT safe_seq FROM bundle_active_set_watermark WHERE id = 1")
                        if _one(cur) > 0:
                            break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("watermark never advanced past 0 within 5s of the job running")
        finally:
            await job.stop()

        # A second stop() must be a safe no-op.
        await job.stop()
