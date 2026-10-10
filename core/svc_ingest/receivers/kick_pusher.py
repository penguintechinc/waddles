r"""KickPusherReceiver -- a `waddle_transports.Transport` for inbound Kick chat receipt.

Real Kick public Pusher WebSocket connection -- Kick's own chat backend
runs on Pusher (`wss://ws-<cluster>.pusher.com/app/<key>?protocol=7&
client=js&version=7.6.0&flash=false`), NOT a port of the legacy
`pysher`-based `trigger/receiver/kick_module_flask/services/chat_client.py`,
which wraps that same Pusher wire protocol behind a callback-driven,
thread-based SDK. This receiver speaks the wire protocol directly over the
shared `websockets` library (already an established dependency in this
repo -- see `libs/waddle_transports/waddle_transports/transports/socket.py`'s
own generic `socket` transport, which this receiver does NOT reuse: that
transport's "connect, then only ever receive" contract has no room for the
subscribe-after-`pusher:connection_established` handshake or the
application-level `pusher:ping`/`pusher:pong` keepalive Kick's own Pusher
protocol requires -- same "own the connector-specific protocol on top of
the raw primitive" precedent `receivers/slack_socket.py`'s module
docstring documents for why it holds its own `slack_sdk` client instead of
routing through `socket` too).

`DEFAULT_PUSHER_KEY` (`"eb1d5f283081a78b932c"`) is Kick's own PUBLIC
Pusher application key -- every Kick chat viewer's browser connects with
this exact same key; it identifies the Pusher APPLICATION (Kick's chat
backend), not a per-caller credential. Kept as a documented config default
(same "public, not secret" status the legacy `chat_client.py`'s own
`DEFAULT_PUSHER_KEY` class constant already treats it as), overridable via
`config["pusher_key"]` only for a future non-default Pusher app/cluster,
never resolved via `resolve_secret`.

One connection per channel, matching `receivers/twitch_irc.py`'s own
per-channel precedent (`community=<channel_slug>`, see that module's own
docstring) -- `app.py`'s startup would build one `KickPusherReceiver` per
configured channel slug, each wrapped in its own `socket_lease.
LeasedReceiver` (out of this PR's scope -- `app.py` is not touched here,
see this module's own test coverage for the receiver in isolation).

RECONNECT-WITH-BACKOFF is deliberately NOT implemented inside this
receiver -- like `TwitchIrcReceiver`/`SlackSocketReceiver`, `receive()`
owns exactly one connection attempt + consume loop; a dropped connection
simply ends the generator (or raises), and `supervisor.ReceiverSupervisor`'s
own restart-on-exit + exponential backoff (the ONE reconnect policy this
codebase uses for every supervised receiver) picks it back up. Re-running
`receive()` re-resolves `channel_slug` -> `chatroom_id` from scratch on
every (re)connect -- one cheap public GET, never cached across
reconnects, so a channel rename/chatroom-id change between reconnects is
picked up automatically rather than needing its own invalidation logic.

Handles the Pusher-protocol-level `pusher:connection_established` (send
the subscribe frame), `pusher:ping` (reply `pusher:pong` -- the
APPLICATION-level Pusher keepalive, independent of and in addition to
`websockets`' own transport-level WebSocket ping/pong frames),
`pusher_internal:subscription_succeeded` (logs `gateway.kick_ready` once
subscription is CONFIRMED, not merely requested), and
`App\\Events\\ChatMessageEvent` (the only chat-message event this
receiver normalizes -- `Subscription`/`GiftedSubscription`/`UserBanned`/
`MessageDeleted` etc., which the legacy `chat_client.py` also bound
handlers for, are out of scope for a chat-message receiver; Kick's own
sub/mod/stream lifecycle events arrive over a separate webhook path --
see `builtin_handlers/kick_ingest.py`'s own module docstring for the verification
helper + handler function this repo's HTTP surface can mount).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx
import websockets
from waddle_transports import (
    Direction,
    NonRetryableTransportError,
    RetryableTransportError,
    Transport,
)
from waddle_transports.url_guard import SSRFError, guarded_request, is_private_host
from websockets.exceptions import ConnectionClosed, InvalidURI, WebSocketException

logger = logging.getLogger(__name__)

#: The `consumes` tag every ingest bundle wanting a raw Kick chat message
#: declares (`builtin_handlers/kick_gateway_manifest.py`'s own `stages.ingest.
#: consumes`) -- this receiver's half of that contract.
CONSUMES_TAG = "kick.message"

#: Kick's own PUBLIC Pusher application key -- see module docstring.
DEFAULT_PUSHER_KEY = "eb1d5f283081a78b932c"  # noqa: S105 - Kick's public client key, not a secret
DEFAULT_CLUSTER = "us2"

_KICK_API_BASE = "https://kick.com/api/v2"
_HTTP_TIMEOUT_S = 10.0
_WS_OPEN_TIMEOUT_S = 10.0

#: Kick's own Pusher event name for one chat message -- see module docstring.
_CHAT_MESSAGE_EVENT = "App\\Events\\ChatMessageEvent"


def _guard_ws_url(url: str) -> None:
    """Re-validate a `ws(s)://` URL's host through the shared SSRF guard.

    Mirrors `waddle_transports.transports.socket._guard_ws_url` exactly --
    that module exports no public helper for this, so this is a
    deliberate, documented duplication of a few lines, not a divergence
    (`url_guard.validate_url()` itself only accepts `http`/`https`
    schemes, so it isn't reusable here as-is).
    """
    hostname = urlparse(url).hostname
    if hostname and is_private_host(hostname):
        raise SSRFError(f"kick pusher URL host {hostname!r} resolves to a disallowed address")


def _parse_frame(raw: str | bytes) -> dict[str, Any] | None:
    """`json.loads` one raw Pusher WebSocket frame; `None` (skipped) if not a JSON object."""
    try:
        parsed: Any = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_pusher_data(raw_data: object) -> dict[str, Any]:
    """JSON-decode a Pusher frame's `data` field -- always a JSON *string* on the real wire.

    `None`/non-`str`/malformed input yields `{}`, never raises -- mirrors
    `receivers/twitch_irc.py::_parse_tags`'s identical "never raise on a
    malformed optional field" contract.
    """
    if not isinstance(raw_data, str) or not raw_data:
        return {}
    try:
        parsed: Any = json.loads(raw_data)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_badges(identity: object) -> list[str]:
    """`sender.identity.badges` (`[{"type": "moderator", ...}, ...]`) -> `["moderator", ...]`.

    Names only, matching `receivers/twitch_irc.py::_parse_badges`'s
    identical "no caller needs the badge tier/text today" scope.
    `identity` missing/malformed (or its `badges` list malformed) yields
    `[]`, never raises.
    """
    if not isinstance(identity, Mapping):
        return []
    raw_badges = identity.get("badges")
    if not isinstance(raw_badges, list):
        return []
    names: list[str] = []
    for badge in raw_badges:
        if isinstance(badge, Mapping):
            badge_type = badge.get("type")
            if isinstance(badge_type, str) and badge_type:
                names.append(badge_type)
    return names


# The ignore comment below suppresses mypy --strict's "cannot subclass Any" complaint --
# Transport resolves to Any since waddle_transports ships no py.typed marker (see
# pyproject.toml's follow_imports="skip" override); the real ABC contract
# (name/directions/receive()) is still honored regardless.
class KickPusherReceiver(Transport):  # type: ignore[misc]
    """One Kick channel's Pusher chat connection per `receive()` call.

    Not platform-level like Discord -- one instance per configured channel
    slug is intended (`community=<slug>`), each wrapped in its own
    `socket_lease.LeasedReceiver`, matching `TwitchIrcReceiver`'s own
    per-channel precedent (see that module's own docstring).
    """

    name: ClassVar[str] = "kick_pusher"
    directions: ClassVar[frozenset[Direction]] = frozenset({Direction.INBOUND})

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        """Build the receiver -- resolves/connects nothing yet, see `receive()`.

        `http_client`, if given, is used as-is for the chatroom lookup and
        never closed by this receiver (caller-owned lifecycle) -- the
        test-only injection point matching `receivers/youtube_live_poll.py`'s
        identical `__init__` precedent. `None` (the real/production path)
        builds and closes its own client for the lifetime of one
        `receive()` call.
        """
        self._injected_http_client = http_client

    async def receive(self, config: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        r"""Resolve `channel_slug` -> chatroom, connect once, yield one dict per chat message.

        `config["channel_slug"]` is required. `config["pusher_key"]`/
        `config["cluster"]` override Kick's own public Pusher app key/
        cluster (see module docstring on why the default is not a
        secret). `config["api_base"]` overrides the channel-lookup REST
        root (test injection; default: the real Kick API).
        `config["chatroom_id"]` skips the lookup outright when already
        known. `config["ws_url"]` overrides the FULL Pusher WebSocket URL
        (test-only escape hatch -- a real local `websockets.serve()` test
        server, matching `libs/waddle_transports/tests/
        test_transport_socket.py`'s own "real local server, not a mocked
        client" testing convention for this same underlying library).

        Real transform (not a stub) of each `App\\Events\\ChatMessageEvent`
        frame into the raw event dict `builtin_handlers/kick_ingest.py::normalize()`
        consumes -- field names here are this receiver's own contract with
        that entrypoint, matching `receivers/twitch_irc.py`'s own
        precedent (no repo-wide "raw platform event" schema exists yet).

        Raises `NonRetryableTransportError` for a missing `channel_slug`,
        an unknown channel (404) or other 4xx from the channel lookup, or
        a malformed/SSRF-rejected WebSocket URL. Raises
        `RetryableTransportError` for a network/5xx failure resolving the
        chatroom, or a dropped/failed WebSocket connection --
        `supervisor.ReceiverSupervisor` owns the actual reconnect-with-
        backoff (see module docstring).
        """
        channel_slug = config.get("channel_slug")
        if not isinstance(channel_slug, str) or not channel_slug:
            raise NonRetryableTransportError("kick pusher config missing required 'channel_slug'")

        if self._injected_http_client is not None:
            chatroom_id = await self._resolve_chatroom_id(
                self._injected_http_client, channel_slug, config
            )
        else:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=False) as client:
                chatroom_id = await self._resolve_chatroom_id(client, channel_slug, config)

        ws_url = config.get("ws_url")
        if not (isinstance(ws_url, str) and ws_url):
            pusher_key = config.get("pusher_key") or DEFAULT_PUSHER_KEY
            cluster = config.get("cluster") or DEFAULT_CLUSTER
            ws_url = (
                f"wss://ws-{cluster}.pusher.com/app/{pusher_key}"
                "?protocol=7&client=js&version=7.6.0&flash=false"
            )
        try:
            _guard_ws_url(ws_url)
        except SSRFError as exc:
            raise NonRetryableTransportError(
                f"kick pusher URL rejected by SSRF guard: {exc}"
            ) from exc

        subscribe_channel = f"chatrooms.{chatroom_id}.v2"
        try:
            async with websockets.connect(ws_url, open_timeout=_WS_OPEN_TIMEOUT_S) as ws:
                async for raw in ws:
                    frame = _parse_frame(raw)
                    if frame is None:
                        continue
                    event = frame.get("event")
                    data = _parse_pusher_data(frame.get("data"))

                    if event == "pusher:connection_established":
                        await ws.send(
                            json.dumps(
                                {
                                    "event": "pusher:subscribe",
                                    "data": {"channel": subscribe_channel},
                                }
                            )
                        )
                        continue
                    if event == "pusher:ping":
                        await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                        continue
                    if (
                        event == "pusher_internal:subscription_succeeded"
                        and frame.get("channel") == subscribe_channel
                    ):
                        logger.info(
                            "gateway.kick_ready channel=%s chatroom=%s",
                            channel_slug,
                            chatroom_id,
                        )
                        continue
                    if event != _CHAT_MESSAGE_EVENT:
                        logger.debug("receiver.skipped_event platform=kick event=%s", event)
                        continue

                    item = self._normalize_chat_message(data, channel_slug, chatroom_id)
                    if item is not None:
                        yield item
        except InvalidURI as exc:
            raise NonRetryableTransportError(f"kick pusher URL is invalid: {exc}") from exc
        except (TimeoutError, OSError, ConnectionClosed, WebSocketException) as exc:
            raise RetryableTransportError(f"kick pusher connection failed: {exc}") from exc

    async def _resolve_chatroom_id(
        self, client: httpx.AsyncClient, channel_slug: str, config: Mapping[str, Any]
    ) -> int | str:
        """`GET {api_base}/channels/{channel_slug}` -> `channel.chatroom.id`.

        `config["chatroom_id"]`, if given, skips the network call outright
        -- test injection; production `app.py` wiring only ever supplies
        `channel_slug`.
        """
        pre_resolved = config.get("chatroom_id")
        if isinstance(pre_resolved, int | str) and pre_resolved:
            return pre_resolved

        api_base = config.get("api_base") or _KICK_API_BASE
        url = f"{api_base}/channels/{channel_slug}"
        try:
            response = await guarded_request(client, "GET", url)
        except SSRFError as exc:
            raise NonRetryableTransportError(
                f"kick channel lookup URL rejected by SSRF guard: {exc}"
            ) from exc
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
            raise RetryableTransportError(f"kick channel lookup failed: {exc}") from exc

        if response.status_code == 404:
            raise NonRetryableTransportError(f"kick channel not found: {channel_slug!r}")
        if response.status_code >= 500:
            raise RetryableTransportError(
                f"kick channel lookup returned HTTP {response.status_code}",
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise NonRetryableTransportError(
                f"kick channel lookup returned HTTP {response.status_code}",
                http_status=response.status_code,
            )

        try:
            body: Any = response.json()
        except ValueError as exc:
            raise NonRetryableTransportError(
                f"kick channel lookup returned an unparsable response body: {exc}"
            ) from exc

        chatroom = body.get("chatroom") if isinstance(body, dict) else None
        chatroom_id = chatroom.get("id") if isinstance(chatroom, dict) else None
        if not isinstance(chatroom_id, int | str) or not chatroom_id:
            raise NonRetryableTransportError(
                f"kick channel {channel_slug!r} has no chatroom id in the API response"
            )
        return chatroom_id

    @staticmethod
    def _normalize_chat_message(
        data: Mapping[str, Any], channel_slug: str, chatroom_id: int | str
    ) -> dict[str, Any] | None:
        """Build the normalized raw event dict for one `ChatMessageEvent`, or `None` if malformed.

        `is_mod`/`is_subscriber`/`is_owner` are read from `sender`'s own
        explicit boolean flags (`is_moderator`/`is_subscriber`/
        `is_channel_owner` -- matching the legacy `models/events.py::
        KickSender` shape) OR'd with the parsed `badges` list membership
        (`"moderator"`/`"subscriber"`/`"broadcaster"`), since real Kick
        payloads are not guaranteed to always populate both -- either
        source being true is enough.
        """
        content = data.get("content")
        if not isinstance(content, str) or not content:
            return None

        sender = data.get("sender")
        sender = sender if isinstance(sender, Mapping) else {}
        sender_id = sender.get("id")
        has_sender_id = isinstance(sender_id, int | str) and sender_id != ""
        badges = _parse_badges(sender.get("identity"))

        return {
            "platform": "kick",
            "text": content.strip(),
            "chatroom_id": chatroom_id,
            "channel_slug": channel_slug,
            "author_id": str(sender_id) if has_sender_id else None,
            "display_name": sender.get("username") or None,
            "badges": badges,
            "is_mod": bool(sender.get("is_moderator")) or "moderator" in badges,
            "is_subscriber": bool(sender.get("is_subscriber")) or "subscriber" in badges,
            "is_owner": bool(sender.get("is_channel_owner")) or "broadcaster" in badges,
            "message_id": data.get("id") or None,
            "created_at": data.get("created_at") or None,
        }
