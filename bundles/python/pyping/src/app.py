"""Minimal, real `!pyping` -> `pong (py)` chat bundle: the Python-SDK
equivalent of `bundles/rust/ping/src/lib.rs`, hand-built (like `ping` was
hand-built with `cargo-component`) rather than routed through
`bundle_compiler` (stubbed, out of scope).

Implements both WIT exports against `wit/waddle-bundle/stage.wit` via
`waddle_sdk`: `transform()` recognizes the exact command `!pyping` in an
inbound `chat.message` payload and rewrites the event into a `pong (py)`
reply on the same platform/channel; `dispatch()` takes that reply and
relays it back over the `relay` host import (granted only to action-stage
bundles, `wit/waddle-bundle/stage.wit` `interface relay`) to the event's
own origin platform (`envelope.event.platform` -- never a fixed provider,
so a Discord `!pyping`'s pong relays to Discord, not Twitch). Non-matching
text produces no reply and no action.

Wired into the compiled component by `_entry_wiring.py` (a hand-authored
stand-in for what `bundle_compiler`'s `generate_entry_wiring()` would emit
from this bundle's own `bundle.yaml`), which `waddle_sdk._component_entry`
imports as `bundle_transform`/`bundle_dispatch`.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import relay
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: The exact command text this bundle reacts to, matched against the
#: trimmed `text` field only -- no prefix/argument parsing, this is a test
#: fixture, not a command framework (mirrors `bundles/rust/ping`).
PING_COMMAND = "!pyping"
#: The reply body sent back for a matching command.
PONG_REPLY = "pong (py)"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!pyping` -> a `pong (py)` reply.

    Returns `None` (no reply, event dropped) for any payload that isn't a
    `chat.message`-shaped dict with a matching `text`, rather than raising --
    this bundle should never fail the pipeline over an event it was never
    meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or text.strip() != PING_COMMAND:
        return None

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": PONG_REPLY, "channel_id": event.payload.get("channel_id")},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result.

    Matches the duck-typed fields `waddle_sdk._component_entry.WitWorld.dispatch`
    reads off a real action bundle's return value (`transport`, `detail`,
    `sub_type`, `http_status`) -- this bundle relays over a queue, not HTTP,
    so `http_status` stays `None` (no explicit success flag; a raised
    exception is `_component_entry`'s only failure signal for this shape).
    """

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the `pong (py)` reply to the event's own origin platform.

    `config`/`http_client` are accepted but unused -- this bundle relays over
    the WIT `relay` host import, never outbound HTTP -- because
    `waddle_sdk._component_entry.WitWorld.dispatch` always calls
    `bundle_dispatch(envelope, config, http_client=...)` with that exact
    signature.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id` (the
            inbound `chat.message` never carried one).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("pong (py) reply requires a channel_id from the inbound chat.message")

    # Always the inbound event's own platform -- never a hardcoded provider,
    # so a Discord-origin `!pyping` relays its pong to Discord, a
    # Twitch-origin one to Twitch, etc. (mirrors bundles/rust/ping's
    # `build_relay` and its own regression test for this exact behavior).
    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", PONG_REPLY)})
    return DispatchResult(transport=provider, detail="relayed")
