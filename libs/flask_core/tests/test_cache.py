"""Tests for flask_core.cache.CacheManager.

No Valkey/Redis server exists in the unit-test environment, so the Redis
*server* is replaced by a small stateful in-memory double implementing the
exact command surface CacheManager uses (GET/SETEX/DEL/EXISTS/MGET/INCRBY/
SCAN/PIPELINE/PING/INFO). CacheManager's own logic -- namespacing, JSON
(de)serialisation, TTL plumbing, fallback selection, error swallowing -- is
exercised for real.
"""

import fnmatch
import json

import pytest

from flask_core import cache as cache_mod
from flask_core.cache import CacheManager, create_cache_manager


class FakeRedisServer:
    """In-memory stand-in for redis.asyncio.Redis (decode_responses=True)."""

    def __init__(self, ping_error: Exception | None = None):
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.ping_error = ping_error
        self.closed = False
        self.info_payload: dict = {}
        self.info_error: Exception | None = None

    async def ping(self):
        if self.ping_error:
            raise self.ping_error
        return True

    async def close(self):
        self.closed = True

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, ttl, value):
        self.data[key] = value
        self.ttls[key] = ttl

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def exists(self, key):
        return 1 if key in self.data else 0

    async def mget(self, keys):
        return [self.data.get(k) for k in keys]

    async def incrby(self, key, amount):
        value = int(self.data.get(key, 0)) + amount
        self.data[key] = str(value)
        return value

    async def scan_iter(self, match=None):
        for key in list(self.data):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def pipeline(self):
        return _FakePipeline(self)

    async def info(self, section):
        if self.info_error:
            raise self.info_error
        return self.info_payload


class _FakePipeline:
    def __init__(self, server: FakeRedisServer):
        self.server = server
        self.ops: list[tuple] = []

    def setex(self, key, ttl, value):
        self.ops.append((key, ttl, value))

    async def execute(self):
        for key, ttl, value in self.ops:
            await self.server.setex(key, ttl, value)


class ExplodingRedis:
    """Every command raises -- verifies each public method swallows + degrades."""

    def __getattr__(self, name):
        async def boom(*a, **k):
            raise ConnectionError(f"{name} unavailable")

        return boom

    def pipeline(self):
        raise ConnectionError("pipeline unavailable")

    def scan_iter(self, match=None):
        raise ConnectionError("scan unavailable")


@pytest.fixture
def server():
    return FakeRedisServer()


@pytest.fixture
def cm(server):
    m = CacheManager(redis_url="redis://x", namespace="ns", default_ttl=42)
    m._redis = server
    m._connected = True
    return m


@pytest.fixture
def mem():
    m = CacheManager(namespace="ns")
    m._fallback_enabled = True
    return m


class TestConnect:
    async def test_no_url_uses_fallback(self):
        m = CacheManager(redis_url=None)
        await m.connect()
        assert m._fallback_enabled and not m._connected

    async def test_redis_package_missing_uses_fallback(self, monkeypatch):
        monkeypatch.setattr(cache_mod, "REDIS_AVAILABLE", False)
        m = CacheManager(redis_url="redis://x")
        await m.connect()
        assert m._fallback_enabled

    async def test_successful_connect(self, monkeypatch, server):
        seen = {}

        def fake_from_url(url, **kwargs):
            seen["url"] = url
            seen["kwargs"] = kwargs
            return server

        monkeypatch.setattr(cache_mod.redis, "from_url", fake_from_url)
        m = CacheManager(redis_url="redis://cache:6379/0", namespace="ns")
        await m.connect()
        assert m._connected and not m._fallback_enabled
        assert seen["url"] == "redis://cache:6379/0"
        assert seen["kwargs"]["decode_responses"] is True

    async def test_connect_failure_falls_back(self, monkeypatch):
        bad = FakeRedisServer(ping_error=ConnectionError("refused"))
        monkeypatch.setattr(cache_mod.redis, "from_url", lambda *a, **k: bad)
        m = CacheManager(redis_url="redis://x", enable_fallback=True)
        await m.connect()
        assert m._fallback_enabled and not m._connected

    async def test_connect_failure_raises_without_fallback(self, monkeypatch):
        bad = FakeRedisServer(ping_error=ConnectionError("refused"))
        monkeypatch.setattr(cache_mod.redis, "from_url", lambda *a, **k: bad)
        m = CacheManager(redis_url="redis://x", enable_fallback=False)
        with pytest.raises(ConnectionError):
            await m.connect()

    async def test_disconnect(self, cm, server):
        await cm.disconnect()
        assert server.closed and not cm._connected

    async def test_disconnect_without_client_is_noop(self):
        await CacheManager().disconnect()


class TestRedisBackedOps:
    async def test_set_get_roundtrip_json_and_namespacing(self, cm, server):
        assert await cm.set("k", {"a": [1, 2]}) is True
        assert "ns:k" in server.data
        assert server.ttls["ns:k"] == 42  # default TTL
        assert await cm.get("k") == {"a": [1, 2]}

    async def test_custom_ttl(self, cm, server):
        await cm.set("k", 1, ttl=7)
        assert server.ttls["ns:k"] == 7

    async def test_get_miss_and_non_json_value(self, cm, server):
        assert await cm.get("missing") is None
        server.data["ns:raw"] = "not-json{"
        assert await cm.get("raw") == "not-json{"

    async def test_delete_and_exists(self, cm):
        await cm.set("k", 1)
        assert await cm.exists("k") is True
        assert await cm.delete("k") is True
        assert await cm.exists("k") is False

    async def test_delete_pattern_only_matching(self, cm, server):
        await cm.set("channels:1", 1)
        await cm.set("channels:2", 2)
        await cm.set("users:1", 3)
        assert await cm.delete_pattern("channels:*") == 2
        assert set(server.data) == {"ns:users:1"}

    async def test_get_many_set_many(self, cm, server):
        assert await cm.set_many({}) is True
        assert await cm.get_many([]) == {}
        assert await cm.set_many({"a": 1, "b": {"x": 2}}, ttl=9) is True
        assert server.ttls["ns:a"] == 9
        server.data["ns:raw"] = "plain"
        got = await cm.get_many(["a", "b", "raw", "none"])
        assert got == {"a": 1, "b": {"x": 2}, "raw": "plain"}

    async def test_increment(self, cm):
        assert await cm.increment("n") == 1
        assert await cm.increment("n", 5) == 6

    async def test_stats_connected(self, cm, server):
        server.info_payload = {"db0": {"keys": 3}, "keyspace_hits": 9, "keyspace_misses": 1}
        stats = await cm.get_stats()
        assert stats["connected"] is True
        assert (stats["redis_keys"], stats["redis_hits"], stats["redis_misses"]) == (3, 9, 1)

    async def test_stats_info_error_is_swallowed(self, cm, server):
        server.info_error = RuntimeError("no info")
        stats = await cm.get_stats()
        assert stats["connected"] is True and "redis_keys" not in stats


class TestNotConnectedNoFallback:
    """Neither connected nor in fallback mode: every op degrades, never raises."""

    async def test_all_ops_degrade(self):
        m = CacheManager(namespace="ns")
        assert await m.get("k") is None
        assert await m.set("k", 1) is False
        assert await m.delete("k") is False
        assert await m.delete_pattern("k*") == 0
        assert await m.exists("k") is False
        assert await m.get_many(["k"]) == {}
        assert await m.set_many({"k": 1}) is False
        assert await m.increment("k") is None


class TestRedisErrorsAreSwallowed:
    @pytest.fixture
    def broken(self):
        m = CacheManager(namespace="ns")
        m._redis = ExplodingRedis()
        m._connected = True
        return m

    async def test_every_op_returns_safe_default(self, broken):
        assert await broken.get("k") is None
        assert await broken.set("k", 1) is False
        assert await broken.delete("k") is False
        assert await broken.delete_pattern("k*") == 0
        assert await broken.exists("k") is False
        assert await broken.get_many(["k"]) == {}
        assert await broken.set_many({"k": 1}) is False
        assert await broken.increment("k") is None

    async def test_set_unserialisable_value_returns_false(self, cm):
        assert await cm.set("k", object()) is False


class TestFallbackMemory:
    async def test_roundtrip_and_exists_delete(self, mem):
        assert await mem.set("a", {"v": 1}) is True
        assert await mem.get("a") == {"v": 1}
        assert await mem.exists("a") is True
        assert await mem.delete("a") is True
        assert await mem.get("a") is None
        assert await mem.exists("a") is False

    async def test_pattern_delete_prefix(self, mem):
        await mem.set_many({"x:1": 1, "x:2": 2, "y:1": 3})
        assert await mem.delete_pattern("x:*") == 2
        assert await mem.get_many(["x:1", "y:1"]) == {"y:1": 3}

    async def test_increment_accumulates(self, mem):
        assert await mem.increment("c") == 1
        assert await mem.increment("c", 4) == 5

    async def test_increment_non_numeric_returns_none(self, mem):
        await mem.set("c", "abc")
        assert await mem.increment("c") is None

    async def test_stats_in_fallback(self, mem):
        await mem.set("a", 1)
        stats = await mem.get_stats()
        assert stats == {
            "namespace": "ns",
            "connected": False,
            "fallback_enabled": True,
            "fallback_size": 1,
        }


class TestCachedDecorator:
    async def test_caches_by_args_and_skips_none(self, mem):
        calls = []

        @mem.cached(ttl=5)
        async def lookup(a, b=2):
            calls.append((a, b))
            return None if a == "none" else {"a": a, "b": b}

        assert await lookup("x", b=3) == {"a": "x", "b": 3}
        assert await lookup("x", b=3) == {"a": "x", "b": 3}
        assert calls == [("x", 3)]  # second served from cache
        assert await lookup("none") is None
        assert await lookup("none") is None
        assert calls.count(("none", 2)) == 2  # None results are never cached
        assert lookup.__name__ == "lookup"

    async def test_custom_key_func(self, mem):
        @mem.cached(key_func=lambda uid: f"user-{uid}")
        async def get_user(uid):
            return {"id": uid}

        await get_user(7)
        assert await mem.exists("user-7")


class TestWarmCache:
    async def test_warm_loads_data(self, mem):
        async def loader():
            return {"a": 1, "b": 2}

        await mem.warm_cache(loader, ["a", "b"])
        assert await mem.get_many(["a", "b"]) == {"a": 1, "b": 2}

    async def test_warm_empty_data_noop(self, mem):
        async def loader():
            return {}

        await mem.warm_cache(loader, [])
        assert (await mem.get_stats())["fallback_size"] == 0

    async def test_warm_loader_failure_swallowed(self, mem):
        async def loader():
            raise RuntimeError("db down")

        await mem.warm_cache(loader, ["a"])  # must not raise


class TestFactory:
    def test_create_cache_manager(self):
        m = create_cache_manager("redis://h:1/0", "svc", default_ttl=11)
        assert (m.redis_url, m.namespace, m.default_ttl, m.enable_fallback) == (
            "redis://h:1/0",
            "svc",
            11,
            True,
        )
        assert json.dumps(m._make_key("k")) == '"svc:k"'
