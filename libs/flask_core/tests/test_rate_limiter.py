"""Tests for flask_core.rate_limiter.RateLimiter -- sliding window, fallback, decorator.

The Redis server is replaced by a stateful sorted-set double (ZADD/ZCARD/
ZREMRANGEBYSCORE/EXPIRE/DEL/SCAN) so the sliding-window *algorithm* in
RateLimiter runs for real, including window expiry driven by timestamps.
"""

import fnmatch
import time

import pytest

from flask_core import rate_limiter as rl
from flask_core.rate_limiter import RateLimiter, RateLimitExceeded, create_rate_limiter


class FakeZSetRedis:
    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.expiry: dict[str, int] = {}
        self.closed = False
        self.ping_error: Exception | None = None

    async def ping(self):
        if self.ping_error:
            raise self.ping_error
        return True

    async def close(self):
        self.closed = True

    async def zremrangebyscore(self, key, lo, hi):
        z = self.zsets.get(key, {})
        lo_v = float("-inf") if lo == "-inf" else float(lo)
        for member in [m for m, s in z.items() if lo_v <= s <= hi]:
            del z[member]

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def expire(self, key, seconds):
        self.expiry[key] = seconds

    async def delete(self, key):
        self.zsets.pop(key, None)

    async def scan_iter(self, match=None):
        for key in list(self.zsets):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


class ExplodingRedis:
    def __getattr__(self, name):
        async def boom(*a, **k):
            raise ConnectionError(name)

        return boom

    def scan_iter(self, match=None):
        raise ConnectionError("scan")


@pytest.fixture
def server():
    return FakeZSetRedis()


@pytest.fixture
def limiter(server):
    r = RateLimiter(redis_url="redis://x", namespace="ns")
    r._redis = server
    r._connected = True
    return r


@pytest.fixture
def mem():
    r = RateLimiter(namespace="ns")
    r._fallback_enabled = True
    return r


class TestConnect:
    async def test_redis_missing_uses_fallback(self, monkeypatch):
        monkeypatch.setattr(rl, "REDIS_AVAILABLE", False)
        r = RateLimiter(redis_url="redis://x")
        await r.connect()
        assert r._fallback_enabled

    async def test_no_url_uses_fallback(self):
        r = RateLimiter()
        await r.connect()
        assert r._fallback_enabled

    async def test_success(self, monkeypatch, server):
        monkeypatch.setattr(rl.redis, "from_url", lambda url, **kw: server)
        r = RateLimiter(redis_url="redis://x")
        await r.connect()
        assert r._connected and not r._fallback_enabled

    async def test_failure_falls_back_and_counts_metric(self, monkeypatch, server):
        server.ping_error = ConnectionError("refused")
        monkeypatch.setattr(rl.redis, "from_url", lambda url, **kw: server)
        incs = []

        class Metric:
            def inc(self):
                incs.append(1)

        monkeypatch.setattr(rl, "_FALLBACK_TOTAL", Metric())
        r = RateLimiter(redis_url="redis://x", enable_fallback=True)
        await r.connect()
        assert r._fallback_enabled and not r._connected and incs == [1]

    async def test_failure_without_metric_still_falls_back(self, monkeypatch, server):
        server.ping_error = ConnectionError("refused")
        monkeypatch.setattr(rl.redis, "from_url", lambda url, **kw: server)
        monkeypatch.setattr(rl, "_FALLBACK_TOTAL", None)
        r = RateLimiter(redis_url="redis://x")
        await r.connect()
        assert r._fallback_enabled

    async def test_failure_raises_without_fallback(self, monkeypatch, server):
        server.ping_error = ConnectionError("refused")
        monkeypatch.setattr(rl.redis, "from_url", lambda url, **kw: server)
        r = RateLimiter(redis_url="redis://x", enable_fallback=False)
        with pytest.raises(ConnectionError):
            await r.connect()

    async def test_disconnect(self, limiter, server):
        await limiter.disconnect()
        assert server.closed and not limiter._connected

    async def test_disconnect_without_client(self):
        await RateLimiter().disconnect()


class TestSlidingWindowRedis:
    async def test_allows_up_to_limit_then_blocks(self, limiter, server):
        results = [await limiter.check_rate_limit("u1", limit=3, window=60) for _ in range(5)]
        assert results == [True, True, True, False, False]
        assert server.expiry["ns:u1"] == 60

    async def test_identifiers_are_independent(self, limiter):
        for _ in range(2):
            assert await limiter.check_rate_limit("a", 2, 60)
        assert not await limiter.check_rate_limit("a", 2, 60)
        assert await limiter.check_rate_limit("b", 2, 60)

    async def test_window_expiry_frees_slots(self, limiter, server):
        old = time.time() - 120
        server.zsets["ns:u"] = {"old1": old, "old2": old + 1}
        assert await limiter.check_rate_limit("u", limit=2, window=60) is True
        assert await limiter.check_rate_limit("u", limit=2, window=60) is True
        assert await limiter.check_rate_limit("u", limit=2, window=60) is False

    async def test_get_remaining(self, limiter):
        assert await limiter.get_remaining("u", 5, 60) == 5
        await limiter.check_rate_limit("u", 5, 60)
        await limiter.check_rate_limit("u", 5, 60)
        assert await limiter.get_remaining("u", 5, 60) == 3

    async def test_remaining_never_negative(self, limiter, server):
        now = time.time()
        server.zsets["ns:u"] = {str(i): now for i in range(10)}
        assert await limiter.get_remaining("u", 5, 60) == 0

    async def test_reset_and_reset_pattern(self, limiter, server):
        for ident in ("user:1", "user:2", "cmd:1"):
            await limiter.check_rate_limit(ident, 5, 60)
        assert await limiter.reset("cmd:1") is True
        assert "ns:cmd:1" not in server.zsets
        assert await limiter.reset_pattern("user:*") == 2
        assert server.zsets == {}

    async def test_stats(self, limiter):
        await limiter.check_rate_limit("a", 5, 60)
        await limiter.check_rate_limit("b", 5, 60)
        stats = await limiter.get_stats()
        assert stats == {
            "namespace": "ns", "connected": True, "fallback_enabled": False,
            "fallback_keys": 0, "active_limits": 2,
        }


class TestDegraded:
    async def test_not_connected_fails_open(self):
        r = RateLimiter()
        assert await r.check_rate_limit("u", 1, 60) is True
        assert await r.get_remaining("u", 7, 60) == 7
        assert await r.reset("u") is False
        assert await r.reset_pattern("u*") == 0

    async def test_redis_errors_fail_open_or_safe_default(self):
        r = RateLimiter(namespace="ns")
        r._redis = ExplodingRedis()
        r._connected = True
        assert await r.check_rate_limit("u", 1, 60) is True  # fail open
        assert await r.get_remaining("u", 4, 60) == 4
        assert await r.reset("u") is False
        assert await r.reset_pattern("u*") == 0
        stats = await r.get_stats()  # scan error swallowed
        assert stats["connected"] is True and "active_limits" not in stats


class TestFallbackMemory:
    async def test_limit_enforced(self, mem):
        assert [await mem.check_rate_limit("u", 2, 60) for _ in range(3)] == [True, True, False]

    async def test_old_timestamps_pruned(self, mem):
        mem._fallback_cache["ns:u"] = [time.time() - 500, time.time() - 400]
        assert await mem.check_rate_limit("u", 1, 60) is True
        assert len(mem._fallback_cache["ns:u"]) == 1

    async def test_remaining(self, mem):
        assert await mem.get_remaining("new", 5, 60) == 5
        await mem.check_rate_limit("u", 5, 60)
        assert await mem.get_remaining("u", 5, 60) == 4
        mem._fallback_cache["ns:u"] = [time.time() - 500]
        assert await mem.get_remaining("u", 5, 60) == 5  # pruned

    async def test_reset(self, mem):
        await mem.check_rate_limit("u", 1, 60)
        assert await mem.reset("u") is True
        assert await mem.check_rate_limit("u", 1, 60) is True

    async def test_reset_pattern(self, mem):
        for ident in ("a:1", "a:2", "b:1"):
            await mem.check_rate_limit(ident, 5, 60)
        assert await mem.reset_pattern("a:*") == 2
        assert list(mem._fallback_cache) == ["ns:b:1"]

    async def test_stats(self, mem):
        await mem.check_rate_limit("u", 5, 60)
        assert await mem.get_stats() == {
            "namespace": "ns", "connected": False, "fallback_enabled": True, "fallback_keys": 1,
        }


class TestDecorator:
    async def test_allows_then_raises(self, mem):
        @mem.limit(identifier_func=lambda user_id: f"user:{user_id}", limit=2, window=60)
        async def action(user_id):
            return f"ok-{user_id}"

        assert await action("7") == "ok-7"
        assert await action("7") == "ok-7"
        with pytest.raises(RateLimitExceeded, match="2 requests per 60s"):
            await action("7")
        assert await action("8") == "ok-8"  # other identifier unaffected

    async def test_custom_exceeded_callback(self, mem):
        async def on_exceeded(user_id):
            return f"slow-down-{user_id}"

        @mem.limit(lambda user_id: user_id, limit=1, window=60, on_limit_exceeded=on_exceeded)
        async def action(user_id):
            return "ok"

        assert await action("x") == "ok"
        assert await action("x") == "slow-down-x"


def test_factory():
    r = create_rate_limiter("redis://h", namespace="zz")
    assert (r.redis_url, r.namespace, r.enable_fallback) == ("redis://h", "zz", True)
