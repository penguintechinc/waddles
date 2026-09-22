"""Tests for `outbound_drain.TwitchOutboundDrain`.

`IrcTransport.send()` is monkeypatched (real socket connection out of
scope for this container's unit tests, same precedent as
`test_receivers_twitch_irc.py`). `redis_client`/`lease_redis_client` are
real `fakeredis.FakeAsyncRedis` instances -- genuine LPUSH/BRPOP/SET/EVAL
round trips, same precedent `test_socket_lease.py` uses for `SocketLease`
itself (this drain now claims one internally -- see `outbound_drain.py`'s
own module docstring for the 2026-09-04 lease-ownership fix).

Any test that calls `drain.run()` uses TWO separate `FakeAsyncRedis`
clients on the SAME `redis_server` (`conftest.py`'s own fixture),
mirroring `outbound_drain.py`'s real `redis_client`/`lease_redis_client`
split -- fail-first proof this split matters: pointing BOTH at the same
single client instead reproduces a genuine hang (confirmed directly
against `fakeredis`, not just theorized) the moment a BRPOP gets
cancelled (lease loss or external cancellation) immediately followed by
the lease's own release EVAL on that same connection -- see
`outbound_drain.py`'s "Two separate Valkey clients" docstring section.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import fakeredis
import pytest
from waddle_transports import TransportResult
from waddle_transports.transports.irc_relay import outbound_queue_key

from outbound_drain import TwitchOutboundDrain
from socket_lease import PLATFORM_COMMUNITY, SocketLease, lease_key
from supervisor import ReceiverSupervisor

_IRC_CONFIG_BASE = {
    "host": "irc.chat.twitch.tv",
    "port": 6697,
    "nick": "waddlebot",
    "password_ref": "TEST_TWITCH_TOKEN_REF",
}


def _drain(
    redis_client: Any,
    *,
    lease_redis_client: Any = None,
    owner_id: str = "replica-a",
    **kwargs: Any,
) -> TwitchOutboundDrain:
    return TwitchOutboundDrain(
        redis_client=redis_client,
        lease_redis_client=lease_redis_client if lease_redis_client is not None else redis_client,
        irc_config_base=_IRC_CONFIG_BASE,
        owner_id=owner_id,
        **kwargs,
    )


class _BrpopRaises:
    """Delegates SET/EVAL to a real fakeredis client (so lease claim succeeds); BRPOP always raises.

    Proves a genuine connection failure during the blocking pop still
    propagates out of `run()` -- see `outbound_drain.py`'s own docstring:
    this module deliberately does NOT blanket-swallow `BRPOP` errors,
    only the clean `None` (idle timeout) result.
    """

    def __init__(self, inner: Any, exc: Exception) -> None:
        self._inner = inner
        self._exc = exc

    async def brpop(self, keys: Any, timeout: Any) -> Any:  # noqa: ASYNC109 - mirrors redis-py's own brpop signature
        raise self._exc


class _BrpopReturnsNoneThenBlocks:
    """First call returns `None` (a clean BRPOP timeout expiry); every call after blocks forever.

    Covers the drain's `popped is None: continue` branch directly and
    deterministically -- `fakeredis`'s own real BRPOP never actually
    times out within a fast test's short sleep window, so this branch is
    otherwise only reachable by waiting out a real multi-second timeout.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def brpop(self, keys: Any, timeout: Any) -> Any:  # noqa: ASYNC109 - mirrors redis-py's own brpop signature
        self.calls += 1
        if self.calls == 1:
            return None
        await asyncio.sleep(999)


class TestSendOne:
    async def test_sends_a_well_formed_message(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drain = _drain(redis_client)
        captured = {}

        async def _fake_send(config, payload):  # noqa: ANN001, ANN202
            captured["config"] = config
            captured["payload"] = payload
            return TransportResult(transport="irc", detail="sent")

        monkeypatch.setattr(drain._irc, "send", _fake_send)  # noqa: SLF001

        await drain._send_one(json.dumps({"channel": "waddlebot", "text": "hi chat"}))  # noqa: SLF001

        assert captured["config"] == {**_IRC_CONFIG_BASE, "channel": "waddlebot"}
        assert captured["payload"] == {"text": "hi chat"}

    async def test_malformed_json_is_dropped(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drain = _drain(redis_client)
        called = False

        async def _fake_send(config, payload):  # noqa: ANN001, ANN202
            nonlocal called
            called = True

        monkeypatch.setattr(drain._irc, "send", _fake_send)  # noqa: SLF001

        await drain._send_one("not json")  # noqa: SLF001 - must not raise
        assert called is False

    async def test_missing_channel_key_is_dropped(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drain = _drain(redis_client)
        called = False

        async def _fake_send(config, payload):  # noqa: ANN001, ANN202
            nonlocal called
            called = True

        monkeypatch.setattr(drain._irc, "send", _fake_send)  # noqa: SLF001

        await drain._send_one(json.dumps({"text": "hi"}))  # noqa: SLF001 - must not raise
        assert called is False

    async def test_send_failure_does_not_propagate(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drain = _drain(redis_client)

        async def _raise(config, payload):  # noqa: ANN001, ANN202, ARG001
            raise ConnectionError("irc unreachable")

        monkeypatch.setattr(drain._irc, "send", _raise)  # noqa: SLF001

        await drain._send_one(json.dumps({"channel": "waddlebot", "text": "hi"}))  # noqa: SLF001


class TestRunStop:
    async def test_run_drains_a_queued_message_end_to_end(
        self, redis_client: Any, redis_server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-first proof: a message relayed via `outbound_queue_key` is delivered.

        Via the same queue `bundles/twitch_send_action.py` LPUSHes onto
        from svc-action. This replica is the only claimant, so it wins
        the lease and drains normally.
        """
        lease_client = fakeredis.FakeAsyncRedis(decode_responses=True, server=redis_server)
        drain = _drain(redis_client, lease_redis_client=lease_client)
        sent: list[tuple[str, str]] = []

        async def _fake_send(config, payload):  # noqa: ANN001, ANN202
            sent.append((config["channel"], payload["text"]))
            return TransportResult(transport="irc", detail="sent")

        monkeypatch.setattr(drain._irc, "send", _fake_send)  # noqa: SLF001

        await redis_client.lpush(
            outbound_queue_key("twitch"), json.dumps({"channel": "waddlebot", "text": "relayed!"})
        )

        # `drain.run()`'s loop blocks on BRPOP with a real (multi-second)
        # timeout when the queue is empty -- `stop()` only takes effect
        # between BRPOP calls, so it can't interrupt one already in
        # flight. Cancel the task directly instead of a graceful stop()
        # to keep this test fast and deterministic.
        task = asyncio.ensure_future(drain.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass

        assert sent == [("waddlebot", "relayed!")]
        await lease_client.aclose()

    async def test_stop_never_raises(self, redis_client: Any) -> None:
        drain = _drain(redis_client)
        await drain.stop()  # must not raise

    async def test_none_result_is_treated_as_idle_and_loops_again(self, redis_client: Any) -> None:
        """A clean BRPOP timeout expiry (`None`) loops the drain, it does not raise or exit."""
        double = _BrpopReturnsNoneThenBlocks()
        drain = _drain(double, lease_redis_client=redis_client)
        task = asyncio.ensure_future(drain.run())
        await asyncio.sleep(0.05)

        assert double.calls >= 2  # looped past the first None result to a second BRPOP call
        assert task.done() is False

        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass

    async def test_idle_empty_queue_does_not_raise_or_exit(
        self, redis_client: Any, redis_server: fakeredis.FakeServer
    ) -> None:
        """Steady state: an idle queue keeps the task alive, no raise, no early return."""
        lease_client = fakeredis.FakeAsyncRedis(decode_responses=True, server=redis_server)
        drain = _drain(redis_client, lease_redis_client=lease_client)
        task = asyncio.ensure_future(drain.run())
        await asyncio.sleep(0.1)
        assert task.done() is False

        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass
        await lease_client.aclose()


class TestLeaseOwnership:
    async def test_non_owner_never_drains_or_sends(
        self, redis_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A replica that loses the claim race never drains -- the queued item stays queued."""
        other = SocketLease(
            provider="twitch",
            community=PLATFORM_COMMUNITY,
            owner_id="replica-a",
            redis_client=redis_client,
        )
        assert await other.try_claim() is True

        drain = _drain(redis_client, owner_id="replica-b")
        called = False

        async def _fake_send(config, payload):  # noqa: ANN001, ANN202, ARG001
            nonlocal called
            called = True

        monkeypatch.setattr(drain._irc, "send", _fake_send)  # noqa: SLF001

        await redis_client.lpush(
            outbound_queue_key("twitch"), json.dumps({"channel": "waddlebot", "text": "hi"})
        )

        await drain.run()  # returns without hanging -- lease unavailable

        assert called is False
        assert await redis_client.llen(outbound_queue_key("twitch")) == 1

    async def test_losing_the_lease_stops_draining(
        self, redis_client: Any, redis_server: fakeredis.FakeServer
    ) -> None:
        """Fail-first proof of failover: lease loss stops the drain loop, releases cleanly."""
        lease_client = fakeredis.FakeAsyncRedis(decode_responses=True, server=redis_server)
        drain = _drain(
            redis_client,
            lease_redis_client=lease_client,
            owner_id="replica-a",
            renew_interval_s=0.01,
        )
        task = asyncio.ensure_future(drain.run())
        await asyncio.sleep(0.03)

        # Simulate the lease expiring and replica-b claiming it (a real
        # TTL lapse in production; forced here via direct key deletion +
        # a fresh claim so the test doesn't wait out a real TTL).
        await redis_client.delete(lease_key("twitch", PLATFORM_COMMUNITY))
        other = SocketLease(
            provider="twitch",
            community=PLATFORM_COMMUNITY,
            owner_id="replica-b",
            redis_client=redis_client,
        )
        assert await other.try_claim() is True

        # replica-a's renew loop should notice on its next tick and let
        # run() return normally rather than hang or raise.
        await asyncio.wait_for(task, timeout=2.0)
        await lease_client.aclose()

    async def test_connection_failure_during_brpop_propagates(self, redis_client: Any) -> None:
        """A genuine BRPOP failure (not an idle timeout) must surface, not be swallowed."""
        flaky = _BrpopRaises(redis_client, ConnectionError("valkey unreachable"))
        drain = _drain(flaky, lease_redis_client=redis_client, owner_id="replica-a")

        with pytest.raises(ConnectionError, match="valkey unreachable"):
            await drain.run()

    async def test_supervisor_does_not_record_a_failure_while_idle(
        self, redis_client: Any, redis_server: fakeredis.FakeServer
    ) -> None:
        """An idle queue must not look like `receiver_failed` to the supervisor -- no backoff."""
        lease_client = fakeredis.FakeAsyncRedis(decode_responses=True, server=redis_server)
        drain = _drain(redis_client, lease_redis_client=lease_client)
        supervisor = ReceiverSupervisor(base_backoff_s=0.01, max_backoff_s=0.05)
        supervisor.register("twitch_outbound_drain", drain.run)

        await supervisor.start()
        await asyncio.sleep(0.2)
        await supervisor.stop()

        assert supervisor.restart_count("twitch_outbound_drain") == 0
        await lease_client.aclose()

    async def test_supervisor_restarts_after_a_genuine_connection_failure(
        self, redis_client: Any
    ) -> None:
        """A real connection failure DOES count as `receiver_failed` -- supervisor restarts it."""
        flaky = _BrpopRaises(redis_client, ConnectionError("valkey unreachable"))
        drain = _drain(flaky, lease_redis_client=redis_client, owner_id="replica-a")
        supervisor = ReceiverSupervisor(base_backoff_s=0.01, max_backoff_s=0.05)
        supervisor.register("twitch_outbound_drain", drain.run)

        await supervisor.start()
        await asyncio.sleep(0.1)
        await supervisor.stop()

        assert supervisor.restart_count("twitch_outbound_drain") >= 1
