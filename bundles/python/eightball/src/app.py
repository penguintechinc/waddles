"""`!8ball <question>` -> a random canned answer, relayed to the caller's own origin platform.

Token-safe (PR batch 1, 2026-10-03): relay only, no kv/db state, never echoes
another user's handle -- safe to ship ahead of the PII-tokenization pipeline
(#427/#429). Modeled on `bundles/python/pyping`'s hand-built structure (same
caveat: routed through a hand-authored `_entry_wiring.py`, not
`bundle_compiler`, which is still stubbed).

Gated behind the PostHog flag ``waddles.command-8ball`` (`critical-rules.md`
Feature Flags -- new flags default OFF until validated) via
``waddle_sdk.flask_core.feature_flags.feature_enabled``, the WIT ``%flags``
import shim. The flag check runs only *after* the command-text match (not
before) so an unrelated event never pays for a host round trip it doesn't
need -- a flag-disabled `!8ball` is indistinguishable from an unrecognized
command: no reply, no action-stage invocation.

Answers drawn via the stdlib ``random`` module: the `stage` WIT world
(`wit/waddle-bundle/stage.wit`) declares no dedicated ``random`` import, but
`sdk/waddle-sdk-cs/src/Random/WaddleRandom.cs`'s own docstring confirms
`wasi:random/random@0.2.6` is pulled in transitively by the language
runtime's own RNG seeding on the `wasi-wasm`/WASI-0.2 target regardless --
the same applies to componentize-py's CPython-on-WASI build, so no SDK-level
WIT binding is needed here either.
"""

from __future__ import annotations

import random
import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention (`critical-rules.md`).
FLAG_KEY = "waddles.command-8ball"

#: Matches `!8ball` alone or `!8ball <anything>` -- the question's content is
#: never inspected (the reply doesn't depend on it), only whether the command
#: itself was invoked.
COMMAND_PATTERN = re.compile(r"^!8ball(?:\s+.*)?$", re.IGNORECASE)

#: Generic, non-trademarked canned responses (affirmative / non-committal / negative).
ANSWERS: tuple[str, ...] = (
    "It is certain.",
    "Without a doubt.",
    "Yes, definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!8ball` -> a random canned-answer reply.

    Returns `None` (no reply, event dropped) for any non-matching payload or
    while the `waddles.command-8ball` flag is disabled -- never raises over
    an event this bundle was never meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or not COMMAND_PATTERN.match(text.strip()):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    answer = random.choice(ANSWERS)  # noqa: S311 - a game reply, not a security decision
    log.info("eightball.transform matched", actor=event.actor, platform=event.platform)

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": f"\U0001f3b1 {answer}", "channel_id": event.payload.get("channel_id")},
        occurred_at=event.occurred_at,
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `pyping`'s own `app.py`."""

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
    """Implement `action-stage.dispatch`: relay the 8-ball reply to the event's own origin platform.

    `config`/`http_client` are accepted but unused -- relays over the WIT
    `relay` host import only, never outbound HTTP.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("8ball reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    text = payload.get("text", "")
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("eightball.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
