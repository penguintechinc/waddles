"""`!wyr` (alias `!wouldyourather`) -> a random SFW "Would you rather X or Y?" prompt.

Stateless static-list relay fun, modeled on `bundles/python/eightball`: no kv/db, no host
capability beyond `flags` (feature gate) and `relay`. The reply never depends on or echoes
user input, and the `event.actor` is never logged -- logs carry only the platform name.

Gated by the `waddles.command-wouldyourather` feature flag (default OFF), checked only after
the command text matched so an unrelated event never pays for a host round trip.
"""

from __future__ import annotations

import random
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-wouldyourather"
SPEC = CommandSpec(name="wyr")
#: Alias spelling of the command; both resolve to the same behavior.
ALIAS_SPEC = CommandSpec(name="wouldyourather")

#: Static, original, family-friendly (option A, option B) pairs.
PROMPTS: tuple[tuple[str, str], ...] = (
    ("be able to fly", "be able to turn invisible"),
    ("always be ten minutes late", "always be twenty minutes early"),
    ("have a pet dragon", "have a pet unicorn"),
    ("live in a treehouse", "live in a houseboat"),
    ("only eat pizza forever", "only eat tacos forever"),
    ("speak every language", "talk to animals"),
    ("never have to sleep", "never have to eat"),
    ("explore the deep ocean", "explore outer space"),
    ("have a rewind button for your life", "have a pause button for your life"),
    ("be the best player on a losing team", "be the worst player on a winning team"),
    ("have unlimited game saves", "have unlimited extra lives"),
    ("always win at rock-paper-scissors", "always win at coin flips"),
    ("have super strength", "have super speed"),
    ("live without music", "live without movies"),
    ("have a penguin as a butler", "have a robot as a butler"),
    ("explore a haunted castle", "explore a jungle temple"),
    ("be a famous streamer", "be a famous chef"),
    ("have winter all year", "have summer all year"),
    ("be able to read minds", "be able to see the future"),
    ("have a time machine to the past", "have a time machine to the future"),
    ("never lag in an online game", "always get the best loot drop"),
    ("have a bottomless bowl of ice cream", "have a bottomless bowl of fries"),
    ("teleport anywhere but only once a day", "run at 100 mph all the time"),
    ("be a wizard", "be a knight"),
    ("always have to whisper", "always have to shout"),
)

USAGE = "Usage: !wyr"


def _matches(text: str) -> bool | None:
    """`True` for a bare valid invocation, `False` for a malformed one, `None` if not ours."""
    head = text.strip().partition(" ")[0].lower()
    for spec in (SPEC, ALIAS_SPEC):
        if head == f"!{spec.name}":
            try:
                parsed = parse_command(text, spec)
            except CommandUsageError:
                return False
            return parsed.option is None and parsed.sub_module is None
    return None


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!wyr` -> a random would-you-rather prompt.

    Returns `None` for a non-matching payload or while the feature flag is off. A recognized
    but malformed invocation (`!wyr foo`) gets a usage reply, never silence.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    matched = _matches(text)
    if matched is None:
        return None
    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    if matched:
        a, b = random.choice(PROMPTS)
        text_out = f"Would you rather {a} or {b}?"
    else:
        text_out = USAGE
    log.info("wouldyourather.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": text_out, "channel_id": event.payload.get("channel_id")},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

    __slots__ = ("detail", "http_status", "sub_type", "transport")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record which provider the reply was relayed to, and a short detail string."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: relay the reply to the event's own origin platform.

    `config`/`http_client` are accepted but unused -- relays over the WIT `relay` host
    import only, never outbound HTTP.

    Raises:
        ValueError: The reply payload has no `channel_id` or no non-empty `text`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "wouldyourather reply requires a channel_id from the inbound chat.message"
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("wouldyourather reply requires non-empty text")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("wouldyourather.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
