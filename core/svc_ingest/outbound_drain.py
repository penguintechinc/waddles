"""TwitchOutboundDrain -- lease-owned OUTBOUND `irc` relay drain.

Realigned (2026-09-03) onto the merged `waddle_transports` library:
`IrcTransport.send()` connects, authenticates, joins, PRIVMSGs, quits,
and closes -- a short-lived connection per call, not a persistent one --
so relaying an outbound send through a receiver's already-open socket
(this connector's original draft) is unnecessary; the real transport
never held a persistent connection to reuse in the first place. The
Valkey-relay hand-off itself is kept (svc-action never holds Twitch
credentials or opens its own IRC connections -- 2026-09-03 coordination
note), it now just resolves to "drain the queue, open a fresh connection
per message" rather than "drain the queue, reuse the receiver's socket".

**Lease-owned (2026-09-04 fix)**: earlier revisions of this module ran
UNLEASED, reasoning that `BRPOP` is atomic so N replicas competing for
the same queue key never double-send. That is still true, but the
product-owner requirement is stronger than "no duplicates": each
provider's outbound transmit path must be owned by exactly one live
replica, matching inbound ingest's own single-owner-per-socket model
(`socket_lease.py`). `run()` now claims a `socket_lease.SocketLease`
before draining anything, using `provider="twitch",
community=PLATFORM_COMMUNITY` -- the underlying relay queue
(`outbound_queue_key("twitch")`, `waddle_transports.transports.
irc_relay`) is provider-scoped, not per-channel (svc-action's own
`twitch_send_action.py` LPUSHes ONE shared queue for every channel), so
the natural lease granularity here is "the whole Twitch outbound path",
reusing the same `PLATFORM_COMMUNITY` sentinel `receivers/
discord_gateway.py`'s own single platform-wide socket already uses --
NOT a literal per-channel lease (that would require a per-channel queue
key, a `waddle_transports`/svc-action change out of this fix's scope).
Registered with `supervisor.ReceiverSupervisor` exactly like the
per-channel `socket_lease.LeasedReceiver`s (`app.py`'s own wiring): a
lease that can't be claimed, or is later lost, makes `run()` return
normally rather than raise, and the supervisor's restart-on-exit +
backoff retries the claim later -- no separate machinery needed.

**Idle-queue false-failure fix (2026-09-04)**: the observed
`supervisor.receiver_failed name=twitch_outbound_drain ... error=Timeout
reading from infra-redis:6379 backoff_s=60.0` was a client/server BRPOP
timeout race, not a real connection loss. `redis-py`'s blocking `BRPOP`
sends its own block timeout (`_POLL_TIMEOUT_S` below) as a *command
argument* the Valkey *server* honors -- it is NOT passed down as the
*client socket's* own read timeout for that call
(`redis.asyncio.connection.PythonParser.read_response`'s `timeout`
kwarg, which BRPOP never supplies, falls back to `self.socket_timeout`).
`redis-py`'s own default `socket_timeout` is 5s
(`redis._defaults.DEFAULT_SOCKET_TIMEOUT`) -- identical to this drain's
own `_POLL_TIMEOUT_S`, so on an idle queue the client's socket read and
the server's BRPOP nil-timeout response raced every single poll cycle;
losing the race raises `redis.exceptions.TimeoutError("Timeout reading
from ...")` client-side instead of a clean `None` return, which
`supervisor.py` (correctly) treats as `receiver_failed` + backoff. Fix:
`app.py` now builds the drain a DEDICATED Valkey connection (not the
app-wide shared client other Twitch/Discord machinery uses) with
`socket_timeout=DRAIN_SOCKET_TIMEOUT_S` -- comfortably above
`_POLL_TIMEOUT_S` -- so BRPOP's own nil-timeout response always wins the
race; the existing `popped is None: continue` below is what actually
handles that clean idle case. A `TimeoutError` that manages to fire
anyway (a read hanging even past the widened margin) is a genuine
problem and is deliberately left to propagate to the supervisor, same as
any other connection failure -- this module does not blanket-swallow it.

**Two separate Valkey clients, deliberately (2026-09-04)**: `redis_client`
(the dedicated, `DRAIN_SOCKET_TIMEOUT_S`-tuned connection above, BRPOP
only) is NEVER reused for `lease_redis_client`'s SET/EVAL calls
(`socket_lease.SocketLease`'s own claim/renew/release), even though both
ultimately point at the same Valkey instance. Cancelling an in-flight
blocking command (`_drain_while_leased`'s own `brpop_task.cancel()` on
lease loss/shutdown) tells asyncio to stop *waiting* for the reply -- it
does NOT tell the Valkey *server* to abandon the command, and `redis-py`'s
`execute_command()` returns that connection to its pool in its own
`finally` regardless of how it exited. A later command drawn from the
same pool can race the server's still-pending (now-stale) BRPOP reply on
that same socket -- confirmed directly against `fakeredis` (which
reproduces the same pooled-connection reuse hazard): the drain's own
`lease.release()` EVAL, issued moments after cancelling a BRPOP on the
SAME client, hung indefinitely; issuing it on a SEPARATE client resolved
instantly. `app.py` wires `lease_redis_client` to the ordinary app-wide
shared `redis_client` (the same one `socket_lease.LeasedReceiver` already
uses for its own lease calls) -- only the blocking BRPOP needs its own
dedicated connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from waddle_transports.transports.irc import IrcTransport
from waddle_transports.transports.irc_relay import outbound_queue_key

from socket_lease import (
    DEFAULT_LEASE_TTL_S,
    DEFAULT_RENEW_INTERVAL_S,
    PLATFORM_COMMUNITY,
    LeaseRedisLike,
    SocketLease,
)

logger = logging.getLogger(__name__)

#: BRPOP's own server-side block timeout (seconds) -- how long one BRPOP
#: call blocks before looping back to check `self._running`/the lease.
_POLL_TIMEOUT_S = 5

#: The drain's DEDICATED Valkey connection's `socket_timeout` (`app.py`'s
#: own client construction) -- must stay strictly greater than
#: `_POLL_TIMEOUT_S` so the client never races BRPOP's own nil-timeout
#: response; see module docstring's "Idle-queue false-failure fix".
DRAIN_SOCKET_TIMEOUT_S = _POLL_TIMEOUT_S + 5.0


class DrainRedisLike(Protocol):
    """The one Valkey method the blocking-pop side needs -- narrow, easy to fake in tests.

    Deliberately does NOT also declare `LeaseRedisLike`'s SET/EVAL --
    `TwitchOutboundDrain.redis_client` (this Protocol) and `.
    lease_redis_client` (`socket_lease.LeaseRedisLike`) are two SEPARATE
    connections on purpose, see module docstring's "Two separate Valkey
    clients" section.

    `keys`/`timeout: Any` -- `redis.asyncio.Redis.brpop`'s real signature
    accepts a broader `KeysT`/`Number | None` than this narrow duck-typed
    contract declares; `Any` here keeps this Protocol satisfied by both
    the real client and a hand-rolled test double, same rationale as
    `fanout.RedisLike`'s own docstring -- including the plain-`def`-
    returning-`Awaitable` shape (see that Protocol's own docstring for
    why `async def` doesn't match `redis-py`'s own stub return type).
    """

    def brpop(  # noqa: ASYNC109 - mirrors redis.asyncio.Redis.brpop's own signature, not a cancellation timeout
        self, keys: Any, timeout: Any
    ) -> Awaitable[Any]:
        """BRPOP across `keys`; returns `(key, value)` or `None` on timeout."""
        ...


@dataclass(slots=True)
class TwitchOutboundDrain:
    """Lease-owned drain of `outbound_queue_key("twitch")`, sending each item via `IrcTransport`.

    Only the replica holding `provider="twitch", community=self.community`
    (`socket_lease.SocketLease`) ever drains or sends -- see module
    docstring. `irc_config_base` carries `{host, port, nick, password_ref,
    use_tls}` -- everything `IrcTransport.send()`'s config needs except
    `channel`, which comes from each dequeued message. `redis_client`
    (BRPOP) and `lease_redis_client` (SET/EVAL) are deliberately two
    separate connections -- see module docstring's "Two separate Valkey
    clients" section for why sharing one is unsafe.
    """

    redis_client: DrainRedisLike
    lease_redis_client: LeaseRedisLike
    irc_config_base: Mapping[str, Any]
    owner_id: str
    community: str = PLATFORM_COMMUNITY
    ttl_s: float = DEFAULT_LEASE_TTL_S
    renew_interval_s: float = DEFAULT_RENEW_INTERVAL_S
    _irc: IrcTransport = field(default_factory=IrcTransport, repr=False)
    _running: bool = field(default=False, repr=False)
    _sleep: Any = field(default=asyncio.sleep, repr=False)

    def _build_lease(self) -> SocketLease:
        return SocketLease(
            provider="twitch",
            community=self.community,
            owner_id=self.owner_id,
            redis_client=self.lease_redis_client,
            ttl_s=self.ttl_s,
        )

    async def run(self) -> None:
        """`ReceiverSupervisor`-compatible entrypoint: claim the lease, then BRPOP + send forever.

        Mirrors `socket_lease.LeasedReceiver.run()`'s own contract: does
        NOT raise when the lease can't be claimed or is later lost -- it
        simply returns, and `ReceiverSupervisor`'s restart-on-exit +
        backoff loop retries claiming later.
        """
        self._running = True
        lease = self._build_lease()
        if not await lease.try_claim():
            logger.info(
                "outbound_drain.not_claimed provider=twitch community=%s owner=%s",
                self.community,
                self.owner_id,
            )
            return

        logger.info(
            "outbound_drain.claimed provider=twitch community=%s owner=%s",
            self.community,
            self.owner_id,
        )
        lease_lost = asyncio.Event()
        renew_task = asyncio.ensure_future(self._renew_loop(lease, lease_lost))
        try:
            await self._drain_while_leased(lease_lost)
        finally:
            renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renew_task
            await lease.release()
            logger.info(
                "outbound_drain.released provider=twitch community=%s owner=%s",
                self.community,
                self.owner_id,
            )

    async def stop(self) -> None:
        """Signal `run()`'s loop to exit after its current BRPOP call. Never raises.

        Not on `app.py`'s own shutdown path today -- `ReceiverSupervisor.
        stop()` cancels the task directly (same as `LeasedReceiver`, which
        has no `stop()` at all) -- kept for callers/tests wanting a
        graceful, between-BRPOP-calls stop instead of an outright
        cancellation.
        """
        self._running = False

    async def _drain_while_leased(self, lease_lost: asyncio.Event) -> None:
        """BRPOP + send, racing every call against `lease_lost` so loss stops draining cleanly."""
        key = outbound_queue_key("twitch")
        lease_lost_task = asyncio.ensure_future(lease_lost.wait())
        try:
            while self._running:
                brpop_task = asyncio.ensure_future(
                    self.redis_client.brpop([key], timeout=_POLL_TIMEOUT_S)
                )
                try:
                    done, _pending = await asyncio.wait(
                        {brpop_task, lease_lost_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                except asyncio.CancelledError:
                    # External cancellation (run()'s own shutdown path) --
                    # asyncio.wait() being cancelled does NOT cancel the
                    # tasks it was waiting on, so brpop_task must be
                    # cleaned up here or it leaks as a never-awaited task.
                    brpop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await brpop_task
                    raise
                if lease_lost_task in done:
                    brpop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await brpop_task
                    return
                # A genuine BRPOP failure (e.g. `redis.exceptions.
                # ConnectionError`) re-raises here via `.result()` -- left
                # to propagate to `run()`/the supervisor deliberately, see
                # module docstring's "Idle-queue false-failure fix".
                popped = brpop_task.result()
                if popped is None:
                    continue  # idle timeout -- steady state, loop again
                _popped_key, raw_value = popped
                await self._send_one(raw_value)
        finally:
            lease_lost_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_lost_task

    async def _renew_loop(self, lease: SocketLease, lease_lost: asyncio.Event) -> None:
        """Renew on `renew_interval_s`; signal `lease_lost` the moment we lose the lease."""
        while True:
            await self._sleep(self.renew_interval_s)
            if not await lease.renew():
                logger.warning(
                    "outbound_drain.lease_lost provider=twitch community=%s owner=%s",
                    self.community,
                    self.owner_id,
                )
                lease_lost.set()
                return

    async def _send_one(self, raw_value: bytes | str) -> None:
        """Parse+send one relayed outbound message; malformed items are dropped, logged."""
        try:
            text = raw_value.decode("utf-8") if isinstance(raw_value, bytes) else raw_value
            data = json.loads(text)
            channel = data["channel"]
            message_text = data["text"]
        except (TypeError, ValueError, KeyError) as exc:
            logger.error("outbound_drain.malformed error=%s", exc)
            return

        config = {**self.irc_config_base, "channel": channel}
        try:
            result = await self._irc.send(config, {"text": message_text})
            logger.debug("outbound_drain.sent channel=%s detail=%s", channel, result.detail)
        except Exception as exc:  # noqa: BLE001 - one bad send must never kill the drain loop
            logger.error("outbound_drain.send_failed channel=%s error=%s", channel, exc)
