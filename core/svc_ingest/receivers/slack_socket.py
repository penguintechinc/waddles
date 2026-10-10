"""SlackSocketReceiver -- a `waddle_transports.Transport` for inbound Slack Socket Mode receipt.

Real `slack_sdk.socket_mode.aiohttp.SocketModeClient` connection: the
app-level token (`xapp-...`) opens the Socket Mode WebSocket itself, the
bot token (`xoxb-...`) builds the `AsyncWebClient` used both for
`auth.test` (this receiver's own bot-token validation + `team_id`
lookup for the `gateway.slack_ready` log line) and, per-call, for the
SDK's internal `apps.connections.open` handshake (which overrides the
client's default token with `app_token` for that one call -- see
`SocketModeClient`'s own `issue_new_wss_url` -> `web_client.
apps_connections_open(app_token=...)`). Deliberately NOT `slack_bolt`
(`trigger/receiver/slack_module/services/slack_bolt_app.py`'s own
dependency) -- this receiver's only job is turning inbound Slack events
into normalized dicts, the same minimal scope `receivers/
discord_gateway.py`/`receivers/twitch_irc.py` already keep; Bolt's
slash-command/modal/interactivity routing is a separate, much larger
migration, out of scope here.

`receive()` bridges the SDK's callback-driven `socket_mode_request_
listeners` dispatch into the ABC's pull-based `AsyncIterator` contract
via an internal `asyncio.Queue`, the same shape `DiscordGatewayReceiver.
receive()` uses for py-cord's `on_message` callback -- see that module's
own docstring.

Deliberate deviation from Discord/Twitch's "connection closed -> iteration
simply ends" contract (that module's own docstring, matching `irc.py`/
`socket.py`): `SocketModeClient.connect()` is NOT a blocking call like
`discord.Bot.start()` -- it returns once the initial handshake succeeds,
and `auto_reconnect_enabled=True` (the SDK's own default, left
unoverridden here) keeps the connection alive across transient drops
entirely inside slack_sdk's own retry loop, never surfacing a "the
connection died" signal back to this receiver. This receiver's own
`receive()` therefore only ever stops on external cancellation (lease
loss / shutdown -- `socket_lease.LeasedReceiver`'s own `generator.
aclose()`), never on its own initiative -- letting `ReceiverSupervisor`
restart-on-exit tear down and rebuild the whole socket (and re-claim the
lease) on every transient network hiccup would be strictly worse than
the SDK's own already-open, already-authenticated reconnect path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar, cast

from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient
from waddle_transports import Direction, NonRetryableTransportError, Transport
from waddle_transports.signing import SecretResolutionError, resolve_secret

logger = logging.getLogger(__name__)

#: The `consumes` tag every ingest bundle wanting a raw Slack event
#: declares (a future `builtin_handlers/slack_gateway_manifest.py`'s own
#: `stages.ingest.consumes` -- not part of this PR's scope, see
#: `builtin_handlers/slack_ingest.py`'s own docstring for the documented gap) --
#: this receiver's half of that contract.
CONSUMES_TAG = "slack.message"

#: `events_api` event `type`s this receiver normalizes -- every other
#: event type (reactions, channel renames, ...) is silently ignored,
#: DEBUG-logged, never raised (an unhandled event type is not an error).
_HANDLED_EVENT_TYPES = frozenset({"message", "app_mention", "member_joined_channel"})

#: `message` event `subtype`s that describe an edit/delete of an ALREADY-
#: fanned-out message, never a new one -- reprocessing these as fresh
#: messages would duplicate downstream side effects (command execution,
#: moderation, ...) for content this receiver already normalized once.
_SKIPPED_MESSAGE_SUBTYPES = frozenset({"message_changed", "message_deleted"})


# The ignore comment below suppresses mypy --strict's "cannot subclass Any" complaint --
# Transport resolves to Any since waddle_transports ships no py.typed marker (see
# pyproject.toml's follow_imports="skip" override); the real ABC contract
# (name/directions/receive()) is still honored regardless.
class SlackSocketReceiver(Transport):  # type: ignore[misc]
    """Holds ONE persistent Slack Socket Mode connection per `receive()` call.

    PLATFORM-level, not per-community: one Socket Mode connection (one
    app-level token) serves every channel/conversation this Slack app is
    installed into -- `socket_lease.LeasedReceiver` (this receiver's own
    caller, see `app.py`) ensures only one live svc-ingest replica ever
    holds an active iteration, matching `DiscordGatewayReceiver`'s
    identical platform-level scope.
    """

    name: ClassVar[str] = "slack_socket"
    directions: ClassVar[frozenset[Direction]] = frozenset({Direction.INBOUND})

    async def receive(self, config: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        """Connect once, yield one normalized dict per inbound (handled) Slack event.

        `config["app_token"]`/`config["bot_token"]` (literal tokens -- test/
        dev convenience) or `config["app_token_ref"]`/`config["bot_token_ref"]`
        (env var *names*, resolved via `resolve_secret` -- the production
        path, never a raw token in bundle/DB config) supply Socket Mode's
        two required credentials.
        """
        app_token = self._resolve_token(
            config, literal_key="app_token", ref_key="app_token_ref", label="app token"
        )
        bot_token = self._resolve_token(
            config, literal_key="bot_token", ref_key="bot_token_ref", label="bot token"
        )
        queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        web_client = AsyncWebClient(token=bot_token)
        client = self._build_client(app_token, web_client, queue)
        try:
            team_id = await self._resolve_team_id(web_client)
            # slack_sdk's own connect()/close() ship no return annotation
            # (real signatures: `async def connect(self):`/`async def
            # close(self):`) -- both are otherwise fully real, awaited
            # async calls, not untyped stubs this receiver wrote itself.
            await client.connect()  # type: ignore[no-untyped-call]
            logger.info("gateway.slack_ready team=%s", team_id)
            while True:
                yield await queue.get()
        finally:
            await client.close()  # type: ignore[no-untyped-call]

    @staticmethod
    def _resolve_token(
        config: Mapping[str, Any], *, literal_key: str, ref_key: str, label: str
    ) -> str:
        token = config.get(literal_key)
        if isinstance(token, str) and token:
            return token
        token_ref = config.get(ref_key)
        if isinstance(token_ref, str) and token_ref:
            try:
                return cast(str, resolve_secret(token_ref))
            except SecretResolutionError as exc:
                raise NonRetryableTransportError(
                    f"slack socket {label} resolution failed: {exc}"
                ) from exc
        raise NonRetryableTransportError(
            f"slack socket config missing required '{literal_key}' or '{ref_key}'"
        )

    @staticmethod
    async def _resolve_team_id(web_client: AsyncWebClient) -> str | None:
        """Validate the bot token (`auth.test`) and return its `team_id`, or raise.

        A failed `auth.test` (invalid/revoked bot token) is a permanent,
        non-retryable failure -- never worth `ReceiverSupervisor` retrying
        with the same bad credential, matching `NonRetryableTransportError`'s
        contract elsewhere in this receiver.
        """
        try:
            auth_info = await web_client.auth_test()
        except SlackApiError as exc:
            raise NonRetryableTransportError(
                f"slack socket bot token validation (auth.test) failed: {exc}"
            ) from exc
        return cast("str | None", auth_info.get("team_id"))

    @staticmethod
    def _build_client(
        app_token: str,
        web_client: AsyncWebClient,
        queue: asyncio.Queue[Mapping[str, Any]],
    ) -> SocketModeClient:
        """Build a real `SocketModeClient` whose request listener pushes dicts onto `queue`.

        The listener is typed against the base `AsyncBaseSocketModeClient`
        (not the concrete `SocketModeClient`) -- `socket_mode_request_
        listeners`'s own declared element type is contravariant in its
        first parameter, so a callback promising to accept only the
        narrower concrete type would not satisfy it.
        """
        client = SocketModeClient(app_token=app_token, web_client=web_client)

        async def _on_request(
            req_client: AsyncBaseSocketModeClient, req: SocketModeRequest
        ) -> None:
            # Ack immediately -- Slack requires a response within 3s of
            # every envelope regardless of whether this receiver
            # ultimately keeps or drops the event (self/bot-authored, an
            # unhandled event type, a skipped edit/delete subtype -- see
            # `_normalize_event`).
            await req_client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id)
            )
            if req.type != "events_api":
                return
            event = req.payload.get("event")
            if not isinstance(event, dict):
                return
            item = SlackSocketReceiver._normalize_event(req.payload, event)
            if item is not None:
                await queue.put(item)

        client.socket_mode_request_listeners.append(_on_request)
        return client

    @staticmethod
    def _normalize_event(
        payload: Mapping[str, Any], event: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Build the normalized dict queued for one `events_api` event, or `None` to drop it.

        Consumed downstream by `builtin_handlers/slack_ingest.py`'s `normalize()` --
        field names here are this receiver's own contract with that
        entrypoint, matching `receivers/discord_gateway.py`'s own
        precedent (no repo-wide "raw platform event" schema exists yet).

        Drops any event carrying a `bot_id` (Slack tags every bot-
        authored message this way, including this app's own sends) --
        "ignore bot/self messages" in one check, never other-bot-vs-self
        distinction (unlike `DiscordGatewayReceiver._is_self`'s
        self-only scope -- Slack's own `bot_id` tagging does not
        distinguish which bot, and reprocessing ANY bot's message here
        risks automation loops this receiver has no way to break).
        `message` events additionally drop `subtype in
        _SKIPPED_MESSAGE_SUBTYPES` (edits/deletes of an already-seen
        message, not a new one). `display_name` is populated ONLY from
        `event.get("username")` (a bot-message override name Slack
        itself puts on the event, when present) -- never an extra
        `users.info` API lookup, matching the "no PII lookups" scope.
        """
        event_type = event.get("type")
        if event_type not in _HANDLED_EVENT_TYPES:
            logger.debug("receiver.skipped_event_type platform=slack event_type=%s", event_type)
            return None
        if event.get("bot_id"):
            logger.debug(
                "receiver.skipped_self platform=slack channel=%s event_type=%s",
                event.get("channel"),
                event_type,
            )
            return None
        subtype = event.get("subtype")
        if event_type == "message" and subtype in _SKIPPED_MESSAGE_SUBTYPES:
            logger.debug("receiver.skipped_subtype platform=slack subtype=%s", subtype)
            return None

        return {
            "platform": "slack",
            "event_type": event_type,
            "text": event.get("text"),
            "channel_id": event.get("channel"),
            "team_id": payload.get("team_id"),
            "thread_ts": event.get("thread_ts"),
            "message_ts": event.get("ts"),
            "platform_user_id": event.get("user"),
            "display_name": event.get("username"),
        }
