"""Tests for flask_core.read_replica -- replica selection, health bookkeeping, read/write routing.

The Postgres server is the only thing doubled: ``psycopg2.connect`` is replaced by a
fake standby connection so the health-check *decision logic* (standby vs primary,
lag thresholds, failure counting) runs for real inside the real thread-pool path.
"""

import asyncio
import sys
import types
from datetime import datetime

import pytest

from flask_core import read_replica as rr
from flask_core.read_replica import (
    ReadReplicaManager,
    ReadReplicaRouter,
    ReplicaConfig,
    ReplicaStatus,
    create_read_replica_manager,
)

PRIMARY = "postgres://app@primary:5432/waddles"


@pytest.fixture
def manager():
    m = ReadReplicaManager(
        PRIMARY,
        replica_configs=[
            ReplicaConfig(host="r1", priority=5),
            ReplicaConfig(host="r2", priority=1),
            ReplicaConfig(host="r3", port=6543, priority=9),
        ],
        health_check_interval=1,
        executor_workers=2,
    )
    yield m
    m.executor.shutdown(wait=False)


def install_fake_psycopg2(monkeypatch, *, standby=True, lag=0.0, connect_error=None):
    """Register a fake psycopg2 whose connection behaves like a (non-)standby server."""

    class Cursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql_text):
            self._last = sql_text

        def fetchone(self):
            if "pg_is_in_recovery" in self._last:
                return (standby,)
            return (lag,)

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return Cursor()

    def connect(**kwargs):
        if connect_error:
            raise connect_error
        connect.kwargs = kwargs
        return Conn()

    mod = types.ModuleType("psycopg2")
    mod.connect = connect
    mod.sql = types.ModuleType("psycopg2.sql")
    monkeypatch.setitem(sys.modules, "psycopg2", mod)
    monkeypatch.setitem(sys.modules, "psycopg2.sql", mod.sql)
    return connect


class TestConstruction:
    def test_from_replica_configs(self, manager):
        assert set(manager.replicas) == {"r1:5432", "r2:5432", "r3:6543"}
        assert all(m.status is ReplicaStatus.UNKNOWN for m in manager.metrics.values())

    def test_from_uris_parses_host_and_port(self):
        m = ReadReplicaManager(PRIMARY, replica_uris=["postgres://u:p@h1:6000/db", "postgresql://h2/db"])
        try:
            assert set(m.replicas) == {"h1:6000", "h2:5432"}
        finally:
            m.executor.shutdown(wait=False)

    def test_no_replicas(self):
        m = ReadReplicaManager(PRIMARY)
        try:
            assert m.replicas == {} and m.get_metrics() == {}
        finally:
            m.executor.shutdown(wait=False)

    def test_uri_without_host_defaults_localhost(self):
        m = ReadReplicaManager(PRIMARY)
        try:
            cfg = m._parse_connection_string("postgres:///db")
            assert (cfg.host, cfg.port) == ("localhost", 5432)
        finally:
            m.executor.shutdown(wait=False)

    def test_unparseable_port_raises(self):
        m = ReadReplicaManager(PRIMARY)
        try:
            with pytest.raises(ValueError):
                m._parse_connection_string("postgres://h:notaport/db")
        finally:
            m.executor.shutdown(wait=False)


class TestReplicaSelection:
    def test_all_unknown_means_no_replica_and_primary_read(self, manager):
        assert manager.get_best_replica() is None
        assert manager.get_read_uri() == PRIMARY

    def test_best_healthy_by_priority(self, manager):
        for rid in ("r1:5432", "r2:5432", "r3:6543"):
            manager.metrics[rid].status = ReplicaStatus.HEALTHY
        assert manager.get_best_replica().host == "r2"  # priority 1 wins
        assert manager.get_read_uri() == "postgres://postgres@r2:5432/postgres"

    def test_unhealthy_skipped(self, manager):
        manager.metrics["r2:5432"].status = ReplicaStatus.UNHEALTHY
        manager.metrics["r1:5432"].status = ReplicaStatus.HEALTHY
        assert manager.get_best_replica().host == "r1"

    def test_degraded_used_only_when_no_healthy(self, manager):
        manager.metrics["r1:5432"].status = ReplicaStatus.DEGRADED
        manager.metrics["r3:6543"].status = ReplicaStatus.DEGRADED
        assert manager.get_best_replica().host == "r1"  # priority 5 < 9
        manager.metrics["r3:6543"].status = ReplicaStatus.HEALTHY
        assert manager.get_best_replica().host == "r3"  # healthy beats better-priority degraded

    def test_write_uri_always_primary(self, manager):
        manager.metrics["r2:5432"].status = ReplicaStatus.HEALTHY
        assert manager.get_write_uri() == PRIMARY


class TestMetrics:
    def test_record_query_and_success_rate(self, manager):
        manager.record_replica_query("r1:5432")
        manager.record_replica_query("r1:5432")
        manager.record_replica_query("r1:5432", success=False)
        manager.record_replica_query("unknown:1")  # ignored
        m = manager.get_metrics()["r1:5432"]
        assert (m["total_queries"], m["failed_queries"]) == (3, 1)
        assert m["success_rate"] == pytest.approx(2 / 3)

    def test_untouched_replica_full_success_rate_and_no_check_time(self, manager):
        m = manager.get_metrics()["r2:5432"]
        assert m["success_rate"] == 1.0 and m["last_health_check"] is None and m["status"] == "unknown"

    def test_last_health_check_serialised(self, manager):
        manager.metrics["r1:5432"].last_health_check = datetime(2026, 1, 2, 3, 4, 5)
        assert manager.get_metrics()["r1:5432"]["last_health_check"] == "2026-01-02T03:04:05"


class TestHealthChecks:
    async def test_healthy_standby(self, manager, monkeypatch):
        connect = install_fake_psycopg2(monkeypatch, standby=True, lag=1.5)
        cfg = manager.replicas["r1:5432"]
        await manager._check_replica_health("r1:5432", cfg)
        m = manager.metrics["r1:5432"]
        assert m.status is ReplicaStatus.HEALTHY
        assert m.replication_lag_seconds == 1.5 and m.consecutive_failures == 0
        assert m.last_health_check is not None
        assert connect.kwargs["host"] == "r1" and connect.kwargs["connect_timeout"] == 5
        assert "statement_timeout=30000" in connect.kwargs["options"]

    async def test_lagging_standby_is_degraded(self, manager, monkeypatch):
        install_fake_psycopg2(monkeypatch, standby=True, lag=120.0)
        await manager._check_replica_health("r1:5432", manager.replicas["r1:5432"])
        m = manager.metrics["r1:5432"]
        assert m.status is ReplicaStatus.DEGRADED and m.consecutive_failures == 1
        assert m.replication_lag_seconds == 120.0

    async def test_null_lag_treated_as_zero(self, manager, monkeypatch):
        install_fake_psycopg2(monkeypatch, standby=True, lag=None)
        await manager._check_replica_health("r1:5432", manager.replicas["r1:5432"])
        assert manager.metrics["r1:5432"].status is ReplicaStatus.HEALTHY

    async def test_non_standby_is_unhealthy(self, manager, monkeypatch):
        install_fake_psycopg2(monkeypatch, standby=False)
        await manager._check_replica_health("r1:5432", manager.replicas["r1:5432"])
        m = manager.metrics["r1:5432"]
        assert m.status is ReplicaStatus.UNHEALTHY and m.consecutive_failures == 1

    async def test_connect_failure_is_unhealthy_and_failures_accumulate(self, manager, monkeypatch):
        install_fake_psycopg2(monkeypatch, connect_error=ConnectionError("refused"))
        cfg = manager.replicas["r1:5432"]
        await manager._check_replica_health("r1:5432", cfg)
        await manager._check_replica_health("r1:5432", cfg)
        m = manager.metrics["r1:5432"]
        assert m.status is ReplicaStatus.UNHEALTHY and m.consecutive_failures == 2

    async def test_recovery_resets_failures(self, manager, monkeypatch):
        cfg = manager.replicas["r1:5432"]
        install_fake_psycopg2(monkeypatch, standby=False)
        await manager._check_replica_health("r1:5432", cfg)
        install_fake_psycopg2(monkeypatch, standby=True, lag=0.1)
        await manager._check_replica_health("r1:5432", cfg)
        m = manager.metrics["r1:5432"]
        assert m.status is ReplicaStatus.HEALTHY and m.consecutive_failures == 0

    async def test_unexpected_error_marks_unknown(self, manager, monkeypatch):
        class BadLoop:
            def run_in_executor(self, *a):
                raise RuntimeError("executor gone")

        monkeypatch.setattr(rr.asyncio, "get_event_loop", lambda: BadLoop())
        await manager._check_replica_health("r1:5432", manager.replicas["r1:5432"])
        assert manager.metrics["r1:5432"].status is ReplicaStatus.UNKNOWN

    async def test_health_loop_checks_all_replicas_then_cancels(self, manager, monkeypatch):
        checked = []

        async def fake_check(rid, cfg):
            checked.append(rid)

        manager.health_check_interval = 0
        monkeypatch.setattr(manager, "_check_replica_health", fake_check)
        await manager.start_health_checks()
        first_task = manager._health_check_task
        await manager.start_health_checks()  # idempotent
        assert manager._health_check_task is first_task
        for _ in range(50):
            if len(set(checked)) == 3:
                break
            await asyncio.sleep(0.01)
        await manager.stop_health_checks()
        assert set(checked) == {"r1:5432", "r2:5432", "r3:6543"}
        assert manager._health_check_task is None

    async def test_health_loop_survives_errors(self, manager, monkeypatch):
        manager.health_check_interval = 0
        calls = {"n": 0}
        real_sleep = asyncio.sleep

        async def flaky_gather(*a, **k):
            calls["n"] += 1
            raise RuntimeError("gather blew up")

        monkeypatch.setattr(rr.asyncio, "gather", flaky_gather)
        await manager.start_health_checks()
        for _ in range(50):
            if calls["n"] >= 2:  # loop kept going after the first exception
                break
            await real_sleep(0.01)
        await manager.stop_health_checks()
        assert calls["n"] >= 2

    async def test_health_loop_with_no_replicas_does_nothing(self):
        m = ReadReplicaManager(PRIMARY, health_check_interval=0)
        try:
            await m.start_health_checks()
            await asyncio.sleep(0.02)
            await m.stop_health_checks()
        finally:
            m.executor.shutdown(wait=False)

    async def test_stop_without_start_is_noop(self, manager):
        await manager.stop_health_checks()

    async def test_cleanup(self, manager):
        await manager.cleanup()
        with pytest.raises(RuntimeError):
            manager.executor.submit(lambda: None)


class FakeDal:
    def __init__(self, uri, fail=False):
        self.uri = uri
        self.fail = fail
        self.calls: list[tuple] = []

    async def select_async(self, query, *a, **k):
        self.calls.append(("select", query))
        if self.fail:
            raise ConnectionError("replica down")
        return [f"rows@{self.uri}"]

    async def count_async(self, query):
        self.calls.append(("count", query))
        if self.fail:
            raise ConnectionError("replica down")
        return 7

    async def insert_async(self, table, **fields):
        self.calls.append(("insert", table, fields))
        return 1

    async def update_async(self, query, **fields):
        self.calls.append(("update", query, fields))
        return 2

    async def delete_async(self, query):
        self.calls.append(("delete", query))
        return 3


class TestRouter:
    def _router(self, manager, *, replica_fail=False):
        primary = FakeDal(PRIMARY)
        replica = FakeDal("postgres://postgres@r2:5432/postgres", fail=replica_fail)
        return ReadReplicaRouter(manager, primary, [replica]), primary, replica

    async def test_reads_go_to_best_replica(self, manager):
        manager.metrics["r2:5432"].status = ReplicaStatus.HEALTHY
        router, primary, replica = self._router(manager)
        assert await router.select_async("q") == ["rows@postgres://postgres@r2:5432/postgres"]
        assert await router.count_async("q") == 7
        assert [c[0] for c in replica.calls] == ["select", "count"] and primary.calls == []

    async def test_reads_use_primary_without_healthy_replica(self, manager):
        router, primary, replica = self._router(manager)
        assert await router.select_async("q") == [f"rows@{PRIMARY}"]
        assert await router.count_async("q") == 7
        assert replica.calls == []

    async def test_reads_use_primary_when_no_replica_dals(self, manager):
        manager.metrics["r2:5432"].status = ReplicaStatus.HEALTHY
        primary = FakeDal(PRIMARY)
        router = ReadReplicaRouter(manager, primary)
        assert await router.select_async("q") == [f"rows@{PRIMARY}"]

    async def test_healthy_replica_without_matching_dal_falls_to_primary(self, manager):
        manager.metrics["r1:5432"].status = ReplicaStatus.HEALTHY  # URI has no DAL
        router, primary, replica = self._router(manager)
        await router.select_async("q")
        await router.count_async("q")
        assert replica.calls == [] and len(primary.calls) == 2

    async def test_replica_failure_retries_on_primary(self, manager):
        manager.metrics["r2:5432"].status = ReplicaStatus.HEALTHY
        router, primary, replica = self._router(manager, replica_fail=True)
        assert await router.select_async("q") == [f"rows@{PRIMARY}"]
        assert await router.count_async("q") == 7
        assert len(replica.calls) == 2 and len(primary.calls) == 2

    async def test_writes_always_primary(self, manager):
        manager.metrics["r2:5432"].status = ReplicaStatus.HEALTHY
        router, primary, replica = self._router(manager)
        assert await router.insert_async("t", a=1) == 1
        assert await router.update_async("q", b=2) == 2
        assert await router.delete_async("q") == 3
        assert [c[0] for c in primary.calls] == ["insert", "update", "delete"]
        assert replica.calls == []


def test_factory_builds_manager():
    m = create_read_replica_manager(PRIMARY, replica_uris=["postgres://x:1/db"], health_check_interval=9)
    try:
        assert m.health_check_interval == 9 and set(m.replicas) == {"x:1"}
    finally:
        m.executor.shutdown(wait=False)
