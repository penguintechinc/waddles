"""Tests for flask_core.message_queue.MessageQueue.

The Redis server is replaced by conftest's stateful ``FakeAsyncRedis`` (real
stream/consumer-group/pending semantics), extended here with XCLAIM and a
controllable idle clock. MessageQueue's own logic -- publish serialisation,
group creation, consume loop, ACK/retry/DLQ decisions, pending reclaim -- runs
for real.
"""

import asyncio
import json

import pytest
import redis as redis_module

from flask_core import message_queue as mq
from flask_core.message_queue import Message, MessageQueue, create_message_queue

from conftest import FakeAsyncRedis


class StreamRedis(FakeAsyncRedis):
    """FakeAsyncRedis + XCLAIM + settable idle time for pending entries."""

    def __init__(self):
        super().__init__()
        self.idle_ms = 0
        self.fail_on: dict[str, Exception] = {}

    def _maybe_fail(self, op):
        if op in self.fail_on:
            raise self.fail_on[op]

    async def xadd(self, *a, **k):
        self._maybe_fail("xadd")
        return await super().xadd(*a, **k)

    async def xgroup_create(self, *a, **k):
        self._maybe_fail("xgroup_create")
        return await super().xgroup_create(*a, **k)

    async def xinfo_stream(self, *a, **k):
        self._maybe_fail("xinfo_stream")
        return await super().xinfo_stream(*a, **k)

    async def xrange(self, *a, **k):
        self._maybe_fail("xrange")
        return await super().xrange(*a, **k)

    async def xpending_range(self, *a, **k):
        self._maybe_fail("xpending_range")
        rows = await super().xpending_range(*a, **k)
        for r in rows:
            r["time_since_delivered"] = self.idle_ms
        return rows

    async def xclaim(self, name, groupname, consumername, min_idle_time, message_ids):
        self._maybe_fail("xclaim")
        stream = self._stream(name)
        claimed = []
        for mid in message_ids:
            for entry_id, fields in stream.entries:
                if entry_id == mid:
                    stream.groups[groupname]["pending"][mid]["consumer"] = consumername
                    claimed.append((entry_id, fields))
        return claimed


@pytest.fixture
def redis_fake():
    return StreamRedis()


@pytest.fixture
def q(redis_fake):
    queue = MessageQueue(redis_url="redis://fake", stream_prefix="p", max_retries=2)
    queue._redis = redis_fake
    queue._connected = True
    return queue


@pytest.fixture
def fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def quick(_delay, *a, **k):
        await real(0)

    monkeypatch.setattr(mq.asyncio, "sleep", quick)


class TestConnection:
    async def test_redis_missing(self, monkeypatch):
        monkeypatch.setattr(mq, "REDIS_AVAILABLE", False)
        m = MessageQueue(redis_url="redis://x", enable_fallback=True)
        await m.connect()
        assert not m._connected

    async def test_no_url(self):
        m = MessageQueue(redis_url=None)
        await m.connect()
        assert not m._connected

    async def test_connect_success(self, monkeypatch, redis_fake):
        monkeypatch.setattr(mq.redis, "from_url", lambda url, **kw: redis_fake)
        m = MessageQueue(redis_url="redis://x")
        await m.connect()
        assert m._connected

    async def test_connect_failure_raises(self, monkeypatch):
        class Bad(StreamRedis):
            async def ping(self):
                raise ConnectionError("refused")

        monkeypatch.setattr(mq.redis, "from_url", lambda url, **kw: Bad())
        m = MessageQueue(redis_url="redis://x")
        with pytest.raises(ConnectionError):
            await m.connect()
        assert not m._connected

    async def test_disconnect_cancels_consumers(self, q):
        async def forever():
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(forever())
        q._consumers["c1"] = task
        await q.disconnect()
        assert task.cancelled()
        assert not q._connected and not q._running

    async def test_disconnect_without_redis(self):
        await MessageQueue().disconnect()

    def test_stream_and_dlq_names(self, q):
        assert q._make_stream_name("e") == "p:stream:e"
        assert q._make_dlq_name("e") == "p:dlq:e"


class TestPublishAndGroups:
    async def test_publish_not_connected(self):
        assert await MessageQueue().publish("s", {"a": 1}) is None

    async def test_publish_serialises_and_returns_id(self, q, redis_fake):
        mid = await q.publish("events", {"a": [1, 2]})
        assert mid
        ((entry_id, fields),) = redis_fake._streams["p:stream:events"].entries
        assert entry_id == mid
        assert json.loads(fields["data"]) == {"a": [1, 2]}
        assert "timestamp" in fields

    async def test_publish_trims_to_max_len(self, q, redis_fake):
        for i in range(5):
            await q.publish("s", {"i": i}, max_len=3)
        assert len(redis_fake._streams["p:stream:s"].entries) == 3

    async def test_publish_error_returns_none(self, q, redis_fake):
        redis_fake.fail_on["xadd"] = RuntimeError("oom")
        assert await q.publish("s", {}) is None

    async def test_publish_unserialisable_returns_none(self, q):
        assert await q.publish("s", {"x": object()}) is None

    async def test_group_not_connected(self):
        assert await MessageQueue().create_consumer_group("s", "g") is False

    async def test_group_create_and_busygroup_idempotent(self, q):
        assert await q.create_consumer_group("s", "g") is True
        assert await q.create_consumer_group("s", "g") is True  # BUSYGROUP -> still True

    async def test_group_other_response_error(self, q, redis_fake):
        redis_fake.fail_on["xgroup_create"] = redis_module.ResponseError("WRONGTYPE nope")
        assert await q.create_consumer_group("s", "g") is False

    async def test_group_generic_error(self, q, redis_fake):
        redis_fake.fail_on["xgroup_create"] = ConnectionError("down")
        assert await q.create_consumer_group("s", "g") is False


class TestProcessing:
    async def _seed(self, q, redis_fake, stream="s", group="g", extra=None):
        await q.create_consumer_group(stream, group)
        fields = {"data": json.dumps({"k": "v"})}
        fields.update(extra or {})
        sname = q._make_stream_name(stream)
        mid = await redis_fake.xadd(sname, fields)
        read = await redis_fake.xreadgroup(group, "c1", {sname: ">"})
        (_, [(mid2, data)]) = read[0]
        assert mid == mid2
        return sname, q._make_dlq_name(stream), mid, data

    async def test_success_acks(self, q, redis_fake):
        sname, dlq, mid, data = await self._seed(q, redis_fake)
        seen = []

        async def handler(m: Message):
            seen.append(m)
            return True

        await q._process_message(sname, dlq, "g", mid, data, handler)
        assert seen[0].data == {"k": "v"} and seen[0].retry_count == 0 and seen[0].stream == sname
        assert redis_fake._streams[sname].groups["g"]["pending"] == {}

    async def test_failure_below_max_stays_pending(self, q, redis_fake):
        sname, dlq, mid, data = await self._seed(q, redis_fake)

        async def handler(m):
            return False

        await q._process_message(sname, dlq, "g", mid, data, handler)
        assert mid in redis_fake._streams[sname].groups["g"]["pending"]
        assert dlq not in redis_fake._streams

    async def test_failure_at_max_goes_to_dlq_and_acks(self, q, redis_fake):
        sname, dlq, mid, data = await self._seed(q, redis_fake, extra={"retry_count": "2"})

        async def handler(m):
            return False

        await q._process_message(sname, dlq, "g", mid, data, handler)
        assert redis_fake._streams[sname].groups["g"]["pending"] == {}
        ((_, dead),) = redis_fake._streams[dlq].entries
        assert dead["original_id"] == mid
        assert dead["failure_reason"] == "max_retries_exceeded"
        assert dead["retry_count"] == "2"

    async def test_handler_exception_leaves_pending(self, q, redis_fake):
        sname, dlq, mid, data = await self._seed(q, redis_fake)

        async def handler(m):
            raise RuntimeError("handler blew up")

        await q._process_message(sname, dlq, "g", mid, data, handler)  # swallowed
        assert mid in redis_fake._streams[sname].groups["g"]["pending"]

    async def test_bad_json_payload_leaves_pending(self, q, redis_fake):
        sname, dlq, mid, data = await self._seed(q, redis_fake)
        data = {"data": "{not json"}

        async def handler(m):
            return True

        await q._process_message(sname, dlq, "g", mid, data, handler)
        assert mid in redis_fake._streams[sname].groups["g"]["pending"]


class TestPendingReclaim:
    async def _pending_one(self, q, redis_fake):
        await q.create_consumer_group("s", "g")
        sname = q._make_stream_name("s")
        await redis_fake.xadd(sname, {"data": json.dumps({"n": 1})})
        await redis_fake.xreadgroup("g", "c1", {sname: ">"})
        return sname, q._make_dlq_name("s")

    async def test_idle_message_is_claimed_and_reprocessed(self, q, redis_fake):
        sname, dlq = await self._pending_one(q, redis_fake)
        redis_fake.idle_ms = 120_000
        seen = []

        async def handler(m):
            seen.append(m.data)
            return True

        await q._process_pending_messages(sname, dlq, "g", "c1", handler)
        assert seen == [{"n": 1}]
        assert redis_fake._streams[sname].groups["g"]["pending"] == {}

    async def test_recent_message_not_claimed(self, q, redis_fake):
        sname, dlq = await self._pending_one(q, redis_fake)
        redis_fake.idle_ms = 10
        seen = []

        async def handler(m):
            seen.append(m)
            return True

        await q._process_pending_messages(sname, dlq, "g", "c1", handler)
        assert seen == []

    async def test_claim_returning_nothing(self, q, redis_fake):
        sname, dlq = await self._pending_one(q, redis_fake)
        redis_fake.idle_ms = 120_000

        async def no_claim(*a, **k):
            return []

        redis_fake.xclaim = no_claim
        seen = []

        async def handler(m):
            seen.append(m)
            return True

        await q._process_pending_messages(sname, dlq, "g", "c1", handler)
        assert seen == []

    async def test_pending_errors_swallowed(self, q, redis_fake):
        redis_fake.fail_on["xpending_range"] = RuntimeError("nope")

        async def handler(m):
            return True

        await q._process_pending_messages("x", "y", "g", "c1", handler)


class TestConsumeLoop:
    async def test_not_connected_returns_immediately(self):
        async def handler(m):
            return True

        await MessageQueue().consume("s", "g", "c", handler)

    async def test_consumes_published_messages_end_to_end(self, q, redis_fake):
        await q.create_consumer_group("events", "g")
        for i in range(3):
            await q.publish("events", {"i": i})
        got = []

        async def handler(m):
            got.append(m.data["i"])
            if len(got) == 3:
                q._running = False
            return True

        await asyncio.wait_for(q.consume("events", "g", "c1", handler), timeout=5)
        assert got == [0, 1, 2]
        assert redis_fake._streams["p:stream:events"].groups["g"]["pending"] == {}

    async def test_loop_error_is_retried(self, q, redis_fake, fast_sleep):
        await q.publish("events", {"i": 1})
        real = redis_fake.xreadgroup
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("blip")
            return await real(*a, **k)

        redis_fake.xreadgroup = flaky
        got = []

        async def handler(m):
            got.append(m.data)
            q._running = False
            return True

        await asyncio.wait_for(q.consume("events", "g", "c1", handler), timeout=5)
        assert got == [{"i": 1}] and calls["n"] >= 2

    async def test_cancelled_inside_loop_exits_cleanly(self, q, redis_fake):
        async def cancelled(*a, **k):
            raise asyncio.CancelledError()

        redis_fake.xreadgroup = cancelled

        async def handler(m):
            return True

        await asyncio.wait_for(q.consume("events", "g", "c1", handler), timeout=5)

    async def test_empty_read_then_pending_pass(self, q, redis_fake):
        """No new messages: loop falls through to the pending pass without calling the handler."""
        calls = {"n": 0}
        real = redis_fake.xreadgroup

        async def read_then_stop(*a, **k):
            calls["n"] += 1
            q._running = False
            return await real(*a, **k)

        redis_fake.xreadgroup = read_then_stop

        async def handler(m):
            raise AssertionError("must not be called")

        await asyncio.wait_for(q.consume("events", "g", "c1", handler), timeout=5)
        assert calls["n"] == 1


class TestIntrospection:
    async def test_stream_info(self, q):
        assert await MessageQueue().get_stream_info("s") is None
        assert await q.get_stream_info("missing") is None  # no such key -> swallowed
        await q.publish("s", {"a": 1})
        info = await q.get_stream_info("s")
        assert info["length"] == 1

    async def test_dlq_messages(self, q, redis_fake):
        assert await MessageQueue().get_dlq_messages("s") == []
        await redis_fake.xadd(
            "p:dlq:s",
            {"data": json.dumps({"x": 1}), "original_id": "9-0", "retry_count": "3",
             "failure_reason": "max_retries_exceeded"},
        )
        (row,) = await q.get_dlq_messages("s", count=10)
        assert row["data"] == {"x": 1}
        assert (row["original_id"], row["retry_count"], row["failure_reason"]) == (
            "9-0", 3, "max_retries_exceeded",
        )

    async def test_dlq_error_returns_empty(self, q, redis_fake):
        redis_fake.fail_on["xrange"] = RuntimeError("x")
        assert await q.get_dlq_messages("s") == []


class TestModels:
    def test_message_to_dict(self):
        m = Message(id="1-0", stream="s", data={"a": 1}, retry_count=2)
        assert m.to_dict() == {"id": "1-0", "stream": "s", "data": {"a": 1}, "retry_count": 2}

    def test_factory(self):
        m = create_message_queue("redis://h", stream_prefix="z", max_retries=9)
        assert (m.redis_url, m.stream_prefix, m.max_retries, m.enable_fallback) == ("redis://h", "z", 9, False)
