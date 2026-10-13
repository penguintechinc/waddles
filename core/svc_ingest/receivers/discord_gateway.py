"""DiscordGatewayReceiver -- a `waddle_transports.Transport` for inbound Discord Gateway receipt.

Real py-cord (`discord.Bot`) connection + intents setup, deliberately
reusing `trigger/receiver/discord_module/services/discord_bot.py`'s own
`_setup_events` shape (default intents + `message_content`/`guilds`, an
`on_ready` log, an `on_message` handler that ignores messages authored
by the bot's OWN identity only -- see `_is_self` -- never other bots,
to leave room for future bot-to-bot features) rather than reimplementing
the py-cord gateway protocol. This receiver does NOT replicate that
legacy module's slash/prefix command
routing (`_register_slash_commands` and friends) -- that command-router
surface is a separate, much larger migration, out of scope here; this
receiver's only job is turning inbound messages into normalized dicts.

NOT `waddle_transports.transports.socket.SocketTransport` -- that
transport is a GENERIC raw-WebSocket client (its own module docstring:
"does not speak Discord Gateway's ... own application-level protocol").
Discord's real gateway protocol (opcodes, heartbeat/ACK, session resume)
needs py-cord, so this is its own `Transport` subclass -- `TransportType.
SOCKET`/`Direction.INBOUND` classification, exactly like `builtin_handlers/
discord_send_action.py` owns its Discord-specific outbound logic instead
of routing through the generic `http` transport's `rest_api` sub_type.

`receive()` bridges py-cord's event-driven dispatch (`on_message`
callbacks) into the ABC's pull-based `AsyncIterator` contract via an
internal `asyncio.Queue`: `on_message` pushes one normalized dict per
inbound message; `receive()` races the bot's own `bot.start()` task
against `queue.get()` and yields whichever completes, until the bot task
itself ends (connection dropped -- surfaced as a raised exception, or a
clean return) or the consuming task is cancelled (this generator's own
`finally` closes the bot; no bespoke `run()`/`stop()` divergence from the
ABC).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar, cast

import discord
from waddle_transports import Direction, NonRetryableTransportError, Transport
from waddle_transports.signing import SecretResolutionError, resolve_secret

logger = logging.getLogger(__name__)

#: The `consumes` tag every ingest bundle wanting a raw Discord message
#: declares (`builtin_handlers/discord_gateway_manifest.py`'s own `stages.ingest.
#: consumes`) -- this receiver's half of that contract.
CONSUMES_TAG = "discord.message"


# The ignore comment below suppresses mypy --strict's "cannot subclass Any" complaint --
# Transport resolves to Any since waddle_transports ships no py.typed marker (see
# pyproject.toml's follow_imports="skip" override); the real ABC contract
# (name/directions/receive()) is still honored regardless.
class DiscordGatewayReceiver(Transport):  # type: ignore[misc]
    """Holds ONE persistent Discord bot gateway connection per `receive()` call.

    PLATFORM-level, not per-community: one `discord.Bot` connection serves
    every guild the bot has been invited to -- `socket_lease.
    LeasedReceiver` (this receiver's own caller, see `app.py`) ensures
    only one live svc-ingest replica ever holds an active iteration.
    """

    name: ClassVar[str] = "discord_gateway"
    directions: ClassVar[frozenset[Direction]] = frozenset({Direction.INBOUND})

    async def receive(self, config: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Connect once, yield one normalized dict per inbound (non-self) Discord message.

        `config["token"]` (a literal token -- test/dev convenience) or
        `config["token_ref"]` (an env var *name*, resolved via
        `resolve_secret` -- the production path, never a raw token in
        bundle/DB config) supplies the bot token.
        """
        token = self._resolve_token(config)
        queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        bot = self._build_bot(queue)
        bot_task = asyncio.ensure_future(bot.start(token))
        try:
            while True:
                queue_task = asyncio.ensure_future(queue.get())
                done, _pending = await asyncio.wait(
                    {bot_task, queue_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if queue_task in done:
                    yield queue_task.result()
                    continue
                # bot_task finished first -- the gateway connection ended
                # (dropped, closed, or never opened). Surface a real
                # failure, if any, then stop iterating; a clean end just
                # ends the generator normally, matching every other
                # waddle_transports `receive()`'s "connection closed ->
                # iteration simply ends" contract (see irc.py/socket.py).
                queue_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await queue_task
                bot_task.result()
                return
        finally:
            if not bot_task.done():
                await bot.close()
                bot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bot_task

    @staticmethod
    def _resolve_token(config: Mapping[str, Any]) -> str:
        token = config.get("token")
        if isinstance(token, str) and token:
            return token
        token_ref = config.get("token_ref")
        if isinstance(token_ref, str) and token_ref:
            try:
                return cast(str, resolve_secret(token_ref))
            except SecretResolutionError as exc:
                raise NonRetryableTransportError(
                    f"discord gateway token resolution failed: {exc}"
                ) from exc
        raise NonRetryableTransportError(
            "discord gateway config missing required 'token' or 'token_ref'"
        )

    @staticmethod
    def _build_bot(queue: asyncio.Queue[Mapping[str, Any]]) -> discord.Bot:
        """Build a real `discord.Bot` whose `on_message` pushes normalized dicts onto `queue`."""
        intents = discord.Intents.default()
        intents.message_content = True  # required to read message text, not just metadata
        intents.guilds = True
        bot = discord.Bot(intents=intents)

        @bot.event  # type: ignore[untyped-decorator]
        async def on_ready() -> None:
            logger.info("gateway.discord_ready user=%s", bot.user)

        @bot.event  # type: ignore[untyped-decorator]
        async def on_message(message: discord.Message) -> None:
            if DiscordGatewayReceiver._is_self(message, bot):
                logger.debug(
                    "receiver.skipped_self platform=discord author_id=%s",
                    message.author.id,
                )
                return
            await queue.put(DiscordGatewayReceiver._build_raw_event(message))

        return bot

    @staticmethod
    def _is_self(message: discord.Message, bot: discord.Bot) -> bool:
        """True only when `message` was authored by THIS bot's own identity.

        Self-only, by id (never `message.author.bot`, which would drop
        every OTHER bot's messages too -- out of scope here; future bot-
        to-bot features may want those). `bot.user` is `None` until the
        gateway handshake completes (before `on_ready`) -- an unknown
        identity errs toward NOT dropping, since a missed self-filter for
        the first few ms of connection is far cheaper than dropping a
        real user's message.
        """
        return bot.user is not None and message.author.id == bot.user.id

    @staticmethod
    def _build_raw_event(message: discord.Message) -> dict[str, Any]:
        """Build the normalized dict yielded by `receive()` for one inbound message.

        Consumed downstream by `core/svc_ingest/builtin_handlers/discord_ingest.py`'s
        `normalize()` -- field names here are this receiver's own contract
        with that entrypoint, not a repo-wide "raw Discord event" schema
        (none exists yet, matching `builtin_handlers/echo_ingest.py`'s own precedent
        of documenting its own minimal shape rather than inventing one).

        `guild_id` is carried for FUTURE use only -- fan-out
        (`app.py`'s wiring, per T9) always routes at `community=None`
        (tenant-wide) today; a real guild->community mapping is a
        deferred, documented follow-up (no such lookup table exists yet
        anywhere in this codebase).
        """
        guild_id = str(message.guild.id) if message.guild else None
        return {
            "platform": "discord",
            "guild_id": guild_id,
            "channel_id": str(message.channel.id),
            "message_id": str(message.id),
            "author_id": str(message.author.id),
            "author_username": message.author.name,
            "content": message.content,
        }
