"""Operational-surface tests for flask_core.stream_pipeline.StreamPipeline.

Complements test_stream_pipeline.py's round-trip tests: configuration/env parsing,
connect/disconnect lifecycle, every degraded (not-connected / Redis-error) branch, DLQ
bounding, introspection helpers.  Redis is conftest's stateful ``FakeAsyncRedis``
(real stream + consumer-group + pending semantics).
"""

import json

import pytest
import redis as redis_module

from conftest import FakeAsyncRedis, _load_stream_pipeline_module

sp = _load_stream_pipeline_module()
StreamPipeline = sp.StreamPipeline


def boom_factory(message="redis exploded"):
    async def boom(*a, **k):
        raise ConnectionError(message)

    return boom


class TestConfiguration:
    def test_env_defaults(self, monkeypatch):
        for k in ("STREAM_PIPELINE_ENABLED", "STREAM_MAX_RETRIES", "STREAM_BATCH_SIZE",
                  "STREAM_BLOCK_MS", "STREAM_DLQ_MAXLEN"):
            monkeypatch.delenv(k, raising=False)
        p = StreamPipeline()
        assert (p.enabled, p.max_retries, p.batch_size, p.block_ms, p.dlq_maxlen) == (
            False, 3, 10, 5000, StreamPipeline.DEFAULT_DLQ_MAXLEN,
        )

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("STREAM_PIPELINE_ENABLED", "yes")
        monkeypatch.setenv("STREAM_MAX_RETRIES", "7")
        monkeypatch.setenv("STREAM_BATCH_SIZE", "25")
        monkeypatch.setenv("STREAM_BLOCK_MS", "100")
        monkeypatch.setenv("STREAM_DLQ_MAXLEN", "55")
        p = StreamPipeline()
        assert (p.enabled, p.max_retries, p.batch_size, p.block_ms, p.dlq_maxlen) == (True, 7, 25, 100, 55)

    def test_explicit_args_beat_env(self, monkeypatch):
        monkeypatch.setenv("STREAM_PIPELINE_ENABLED", "true")
        monkeypatch.setenv("STREAM_MAX_RETRIES", "9")
        p = StreamPipeline(enabled=False, max_retries=1, batch_size=2, block_ms=3, dlq_maxlen=4)
        assert (p.enabled, p.max_retries, p.batch_size, p.block_ms, p.dlq_maxlen) == (False, 1, 2, 3, 4)

    @pytest.mark.parametrize("raw,default,expected", [
        ("true", False, True), ("1", False, True), ("ON", False, True),
        ("false", True, False), ("0", True, False), ("Off", True, False),
        ("", True, True), ("maybe", False, False), ("maybe", True, True),
    ])
    def test_get_env_bool(self, monkeypatch, raw, default, expected):
        monkeypatch.setenv("X_FLAG", raw)
        assert StreamPipeline._get_env_bool("X_FLAG", default) is expected

    def test_stream_and_dlq_naming(self):
        p = StreamPipeline(stream_prefix="s", dlq_prefix="d")
        assert p._make_stream_name("events:process") == "s:events:process"
        assert p._make_dlq_name(p._make_stream_name("events:process")) == "d:events:process"

    def test_factory(self):
        p = sp.create_stream_pipeline("redis://h", stream_prefix="a", dlq_prefix="b", max_retries=1,
                                      batch_size=2, block_ms=3, enabled=True, dlq_maxlen=4)
        assert (p.redis_url, p.stream_prefix, p.dlq_prefix, p.enabled, p.dlq_maxlen) == ("redis://h", "a", "b", True, 4)

    def test_stream_event_to_dict(self):
        e = sp.StreamEvent(id="1-0", stream="s", data={"a": 1}, retry_count=2, timestamp="t")
        assert e.to_dict() == {"id": "1-0", "stream": "s", "data": {"a": 1}, "retry_count": 2, "timestamp": "t"}


class TestLifecycle:
    async def test_connect_disabled_is_noop(self):
        p = StreamPipeline(redis_url="redis://x", enabled=False)
        await p.connect()
        assert not p._connected

    async def test_connect_without_redis_package_disables(self, monkeypatch):
        monkeypatch.setattr(sp, "REDIS_AVAILABLE", False)
        p = StreamPipeline(redis_url="redis://x", enabled=True)
        await p.connect()
        assert p.enabled is False

    async def test_connect_without_url_disables(self):
        p = StreamPipeline(redis_url=None, enabled=True)
        await p.connect()
        assert p.enabled is False

    async def test_connect_success_and_disconnect(self, monkeypatch):
        fake = FakeAsyncRedis()
        closed = []

        async def close():
            closed.append(True)

        fake.close = close
        monkeypatch.setattr(sp.redis, "from_url", lambda url, **kw: fake)
        p = StreamPipeline(redis_url="redis://x", enabled=True)
        await p.connect()
        assert p._connected
        await p.disconnect()
        assert closed == [True] and not p._connected and not p._running

    async def test_connect_failure_disables_and_raises(self, monkeypatch):
        bad = FakeAsyncRedis()
        bad.ping = boom_factory("refused")
        monkeypatch.setattr(sp.redis, "from_url", lambda url, **kw: bad)
        p = StreamPipeline(redis_url="redis://x", enabled=True)
        with pytest.raises(ConnectionError):
            await p.connect()
        assert p.enabled is False and not p._connected

    async def test_disconnect_without_client(self):
        await StreamPipeline().disconnect()


class TestNotConnected:
    async def test_every_operation_degrades_safely(self):
        p = StreamPipeline(enabled=True)
        assert await p.publish_event("s", {}) is None
        assert await p.consume_events("s", "g", "c") == []
        assert await p.acknowledge_event("s", "g", "1-0") is False
        assert await p.move_to_dlq("s", "1-0", "why") is False
        assert await p.create_consumer_group("s", "g") is False
        assert await p.get_pending_events("s", "g") == []
        assert await p.get_stream_info("s") is None
        assert await p.get_dlq_events("s") == []
        assert await p.trim_stream("s") is False


class TestEventOperations:
    async def test_publish_serialises_and_trims(self, pipeline, fake_redis):
        for i in range(5):
            await pipeline.publish_event("events:process", {"i": i}, max_len=3)
        entries = fake_redis._streams["waddlebot:stream:events:process"].entries
        assert len(entries) == 3
        assert json.loads(entries[-1][1]["data"]) == {"i": 4}
        assert entries[-1][1]["retry_count"] == "0" and "timestamp" in entries[-1][1]

    async def test_publish_error_and_unserialisable(self, pipeline, fake_redis, monkeypatch):
        assert await pipeline.publish_event("s", {"x": object()}) is None
        monkeypatch.setattr(fake_redis, "xadd", boom_factory())
        assert await pipeline.publish_event("s", {"ok": 1}) is None

    async def test_consume_batch_respects_count_and_skips_bad_payloads(self, pipeline, fake_redis):
        sname = "waddlebot:stream:events:process"
        await pipeline.create_consumer_group("events:process", "g")
        await fake_redis.xadd(sname, {"data": json.dumps({"n": 1}), "retry_count": "2", "timestamp": "t1"})
        await fake_redis.xadd(sname, {"data": "{broken", "retry_count": "0"})
        await fake_redis.xadd(sname, {"data": json.dumps({"n": 3})})
        events = await pipeline.consume_events("events:process", "g", "c1", count=10)
        assert [e["data"]["n"] for e in events] == [1, 3]  # corrupt one skipped, not fatal
        assert events[0]["retry_count"] == 2 and events[0]["timestamp"] == "t1"
        assert events[0]["stream"] == sname
        assert events[1]["retry_count"] == 0

    async def test_consume_uses_default_batch_size(self, pipeline, fake_redis):
        pipeline.batch_size = 2
        for i in range(5):
            await pipeline.publish_event("s", {"i": i})
        events = await pipeline.consume_events("s", "g", "c1")
        assert len(events) == 2

    async def test_consume_empty_and_error(self, pipeline, fake_redis, monkeypatch):
        assert await pipeline.consume_events("s", "g", "c1") == []
        monkeypatch.setattr(fake_redis, "xreadgroup", boom_factory())
        assert await pipeline.consume_events("s", "g", "c1") == []

    async def test_acknowledge_paths(self, pipeline, fake_redis, monkeypatch):
        await pipeline.publish_event("s", {"a": 1})
        (event,) = await pipeline.consume_events("s", "g", "c1")
        assert await pipeline.acknowledge_event("s", "g", event["id"]) is True
        assert await pipeline.acknowledge_event("s", "g", event["id"]) is False  # already ACKed
        monkeypatch.setattr(fake_redis, "xack", boom_factory())
        assert await pipeline.acknowledge_event("s", "g", "1-0") is False


class TestDeadLetterQueue:
    async def test_move_to_dlq_with_and_without_data(self, pipeline, fake_redis):
        assert await pipeline.move_to_dlq("s", "1-0", "bad", event_data={"k": "v"}, retry_count=3) is True
        assert await pipeline.move_to_dlq("s", "2-0", "worse") is True
        dlq = fake_redis._streams["waddlebot:dlq:s"].entries
        assert json.loads(dlq[0][1]["data"]) == {"k": "v"}
        assert dlq[0][1]["retry_count"] == "3" and dlq[0][1]["failure_reason"] == "bad"
        assert dlq[0][1]["original_stream"] == "waddlebot:stream:s"
        assert "data" not in dlq[1][1]

    async def test_dlq_is_bounded(self, pipeline, fake_redis):
        pipeline.dlq_maxlen = 2
        for i in range(5):
            await pipeline.move_to_dlq("s", f"{i}-0", "poison")
        assert len(fake_redis._streams["waddlebot:dlq:s"].entries) == 2

    async def test_move_to_dlq_error(self, pipeline, fake_redis, monkeypatch):
        monkeypatch.setattr(fake_redis, "xadd", boom_factory())
        assert await pipeline.move_to_dlq("s", "1-0", "x") is False

    async def test_get_dlq_events(self, pipeline, fake_redis):
        await pipeline.move_to_dlq("s", "1-0", "bad", event_data={"k": 1}, retry_count=2)
        await pipeline.move_to_dlq("s", "2-0", "nodata")
        await fake_redis.xadd("waddlebot:dlq:s", {"original_id": "3-0", "data": "not-json", "retry_count": "1"})
        events = await pipeline.get_dlq_events("s", count=10)
        assert events[0]["data"] == {"k": 1} and events[0]["retry_count"] == 2
        assert events[0]["original_id"] == "1-0" and events[0]["failure_reason"] == "bad"
        assert "data" not in events[1]
        assert events[2]["data"] == "not-json"

    async def test_get_dlq_events_error(self, pipeline, fake_redis, monkeypatch):
        monkeypatch.setattr(fake_redis, "xrange", boom_factory())
        assert await pipeline.get_dlq_events("s") == []


class TestGroupsAndIntrospection:
    async def test_group_create_busy_and_errors(self, pipeline, fake_redis, monkeypatch):
        assert await pipeline.create_consumer_group("s", "g") is True
        assert await pipeline.create_consumer_group("s", "g") is True  # BUSYGROUP
        monkeypatch.setattr(fake_redis, "xgroup_create",
                            _raiser(redis_module.ResponseError("WRONGTYPE nope")))
        assert await pipeline.create_consumer_group("s", "g2") is False
        monkeypatch.setattr(fake_redis, "xgroup_create", boom_factory())
        assert await pipeline.create_consumer_group("s", "g3") is False

    async def test_pending_events(self, pipeline, fake_redis, monkeypatch):
        await pipeline.publish_event("s", {"a": 1})
        await pipeline.consume_events("s", "g", "c1")
        (row,) = await pipeline.get_pending_events("s", "g", consumer_name="c1")
        assert row["consumer"] == "c1" and row["delivery_count"] == 1 and row["idle_time_ms"] == 0
        assert await pipeline.get_pending_events("s", "g", consumer_name="other") == []
        monkeypatch.setattr(fake_redis, "xpending_range", boom_factory())
        assert await pipeline.get_pending_events("s", "g") == []

    async def test_stream_info(self, pipeline, fake_redis, monkeypatch):
        assert await pipeline.get_stream_info("missing") == {"length": 0, "exists": False}
        await pipeline.publish_event("s", {"a": 1})
        info = await pipeline.get_stream_info("s")
        assert info["length"] == 1 and info["first_entry"][0] == info["last_generated_id"]
        monkeypatch.setattr(fake_redis, "xinfo_stream", _raiser(redis_module.ResponseError("WRONGTYPE x")))
        assert await pipeline.get_stream_info("s") is None
        monkeypatch.setattr(fake_redis, "xinfo_stream", boom_factory())
        assert await pipeline.get_stream_info("s") is None

    async def test_trim(self, pipeline, fake_redis, monkeypatch):
        for i in range(10):
            await pipeline.publish_event("s", {"i": i})
        assert await pipeline.trim_stream("s", max_len=4) is True
        assert len(fake_redis._streams["waddlebot:stream:s"].entries) == 4
        monkeypatch.setattr(fake_redis, "xtrim", boom_factory())
        assert await pipeline.trim_stream("s") is False


def _raiser(exc):
    async def raise_it(*a, **k):
        raise exc

    return raise_it
