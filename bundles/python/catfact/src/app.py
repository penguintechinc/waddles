"""`!catfact` -> a random cat fact, relayed to the caller's own origin platform.

Stateless and self-contained: relay only, no kv/db state and NO external API calls -- every
entry comes from the curated, wholesome, original `CATFACTS` list baked into this module. Never
echoes or logs `event.actor` or any user text (PII-free logging: platform/count only).
Structure mirrors `bundles/python/eightball` (hand-authored `_entry_wiring.py`, not
`bundle_compiler`).

Gated behind the PostHog flag ``waddles.command-catfact`` (new flags default OFF) via
``waddle_sdk.flask_core.feature_flags.feature_enabled``; the flag check runs only after the
cheap command-text match, so an unrelated event never pays for a host round trip. A
flag-disabled `!catfact` is indistinguishable from an unrecognized command.

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
FLAG_KEY = "waddles.command-catfact"

#: Matches `!catfact` alone or followed by anything -- trailing text is never inspected.
COMMAND_PATTERN = re.compile(r"^!catfact(?:\s+.*)?$", re.IGNORECASE)

#: Reply prefix emoji.
_PREFIX = "🐱"

#: Curated, original, wholesome built-in entries.
CATFACTS: tuple[str, ...] = (
    "Cats sleep for about 12 to 16 hours a day.",
    "A group of cats is called a clowder.",
    "Cats have a flexible spine and collarbone arrangement that helps them land on their feet.",
    "A cat's nose print is unique, much like a human fingerprint.",
    "Cats have 32 muscles in each ear, letting them rotate their ears independently.",
    "Adult cats have 30 teeth; kittens have 26 baby teeth.",
    "A cat's purr vibrates at a frequency between 25 and 150 hertz.",
    "Cats can rotate their ears nearly 180 degrees.",
    "A kitten's eyes are blue at birth and often change color as it grows.",
    "Cats have a third eyelid called the nictitating membrane.",
    "Most cats have about 24 whiskers, arranged in four rows on each side of the nose.",
    "Cats spend a big part of their waking hours grooming themselves.",
    "A cat's sense of smell is roughly 14 times stronger than a human's.",
    "Cats can jump up to about six times their own body length.",
    "The slow blink from a cat is often called a cat kiss, a sign of trust.",
    "Cats use their whiskers to help judge whether they can fit through a gap.",
    "Cats walk by moving both legs on one side, then both legs on the other, like camels and giraffes.",
    "A female cat is called a queen and a male cat is called a tom.",
    "Cats cannot taste sweetness because they lack the receptor for it.",
    "A cat's rough tongue is covered in tiny backward-facing hooks called papillae.",
    "Cats have excellent night vision and need only about one sixth of the light humans need.",
    "Kittens begin to purr when they are only a few days old.",
    "Cats knead soft surfaces with their paws, a habit that starts in kittenhood.",
    "A cat's heart beats about twice as fast as a human heart.",
    "Cats make over 100 different vocal sounds, while dogs make about 10.",
    "Cats mostly meow to communicate with humans, rarely with other cats.",
    "The domestic cat's scientific name is Felis catus.",
    "Cats have a special organ in the roof of their mouth for tasting scents, called the Jacobson's organ.",
    "A cat's tail helps it keep its balance when climbing and leaping.",
    "Cats have been living alongside humans for thousands of years.",
    "Cats can run at speeds of around 30 miles per hour in short bursts.",
    "Many cats are lactose intolerant, so milk is not actually a great treat for them.",
)


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!catfact` -> a random built-in entry.

    Returns `None` for any non-matching payload or while the flag is disabled -- never raises
    over an event this bundle was never meant to react to.
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or not COMMAND_PATTERN.match(text.strip()):
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    entry = random.choice(CATFACTS)  # noqa: S311 - a fun reply, not a security decision
    log.info("catfact.transform matched", platform=event.platform, entries=len(CATFACTS))
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": f"{_PREFIX} {entry}", "channel_id": event.payload.get("channel_id")},
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
    """Implement `action-stage.dispatch`: relay the reply to the event's own origin platform.

    `config`/`http_client` are accepted but unused -- relays over the WIT `relay` host import
    only, never outbound HTTP.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("catfact reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    text = payload.get("text", "")
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("catfact.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
