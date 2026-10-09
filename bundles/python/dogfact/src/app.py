"""`!dogfact` -> a random dog fact, relayed to the caller's own origin platform.

Stateless and self-contained: relay only, no kv/db state and NO external API calls -- every
entry comes from the curated, wholesome, original `DOGFACTS` list baked into this module. Never
echoes or logs `event.actor` or any user text (PII-free logging: platform/count only).
Structure mirrors `bundles/python/eightball` (hand-authored `_entry_wiring.py`, not
`bundle_compiler`).

Gated behind the PostHog flag ``waddles.command-dogfact`` (new flags default OFF) via
``waddle_sdk.flask_core.feature_flags.feature_enabled``; the flag check runs only after the
cheap command-text match, so an unrelated event never pays for a host round trip. A
flag-disabled `!dogfact` is indistinguishable from an unrecognized command.

Entries are drawn with the stdlib ``random`` module -- see `eightball`'s own docstring for why
no dedicated WIT random import is needed.
"""

from __future__ import annotations

import random
import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention.
FLAG_KEY = "waddles.command-dogfact"

#: Matches `!dogfact` alone or followed by anything -- trailing text is never inspected.
COMMAND_PATTERN = re.compile(r"^!dogfact(?:\s+.*)?$", re.IGNORECASE)

#: Reply prefix emoji.
_PREFIX = "🐶"

#: Curated, original, wholesome built-in entries.
DOGFACTS: tuple[str, ...] = (
    "A dog's sense of smell is thousands of times more sensitive than a human's.",
    "Dogs have about 300 million scent receptors in their noses, compared with roughly 6 million in people.",
    "A dog's nose print is unique, just like a human fingerprint.",
    "Puppies are born deaf and blind, and open their eyes after about two weeks.",
    "Dogs can understand many words and gestures from their owners.",
    "Adult dogs have 42 teeth, while puppies have 28 baby teeth.",
    "Dogs sweat mainly through the pads of their paws and cool off by panting.",
    "A wagging tail does not always mean happy: the speed and height of the wag carry meaning too.",
    "Dogs have three eyelids, including a third one that helps keep the eye moist and protected.",
    "The Basenji is known as the barkless dog, though it makes yodel-like sounds.",
    "Greyhounds can reach speeds of about 45 miles per hour.",
    "Dogs can hear sounds at much higher frequencies than people can.",
    "Newborn puppies sleep about 90 percent of the day.",
    "Dalmatian puppies are born completely white and develop their spots later.",
    "Dogs are descended from wolves and were among the first animals domesticated by humans.",
    "A dog's whiskers help it sense nearby objects and air movement.",
    "Dogs dream, and twitching or soft barking during sleep is a sign of it.",
    "Newfoundlands have webbed feet and are excellent swimmers.",
    "Dogs curl up to sleep to protect their vital organs and keep warm.",
    "The Labrador Retriever has been one of the most popular dog breeds for many years.",
    "Dogs can learn to recognize human emotions from faces and voices.",
    "A dog's normal body temperature is about 101 to 102.5 degrees Fahrenheit.",
    "Dogs tilt their heads to better hear and locate sounds, and perhaps to see our faces better.",
    "Many dogs can learn over a hundred words and commands.",
    "Dogs have been trained to help people as guide dogs, search and rescue dogs, and therapy dogs.",
    "Puppies usually need far more sleep than adult dogs.",
    "A dog's sense of smell lets it detect certain scents even when diluted in huge amounts of air.",
    "The Saint Bernard breed was famously used for alpine rescue work.",
    "Dogs mostly see in shades of blue and yellow, and have trouble telling red from green.",
    "Border Collies are widely regarded as among the smartest dog breeds.",
    "Dogs greet each other and people by sniffing, which tells them a lot about who they are meeting.",
    "A dog's paw pads help cushion its steps and give it grip on many surfaces.",
)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!dogfact` -> a random built-in entry.

    Returns `None` for any non-matching payload or while the flag is disabled -- never raises
    over an event this bundle was never meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or not COMMAND_PATTERN.match(text.strip()):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    entry = random.choice(DOGFACTS)
    log.info("dogfact.transform matched", platform=event.platform, entries=len(DOGFACTS))
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": f"{_PREFIX} {entry}", "channel_id": event.payload.get("channel_id")},
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

    `config`/`http_client` are accepted but unused -- relays over the WIT `relay` host import
    only, never outbound HTTP.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("dogfact reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    text = payload.get("text", "")
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("dogfact.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
