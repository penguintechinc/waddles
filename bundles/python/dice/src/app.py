"""`!dice [NdM]` -> a bounds-validated dice roll, relayed to the caller's own origin platform.

Token-safe (PR batch 1, 2026-10-03): relay only, no kv/db state, never echoes
another user's handle, and never logs `event.actor` either -- ahead of the
PII-tokenization pipeline (#427/#429), `event.actor` may currently be a raw
username rather than an opaque token. Modeled on `bundles/python/pyping`'s
hand-built structure (see that module's own docstring for the
`_entry_wiring.py` caveat).

Gated behind the PostHog flag ``waddles.command-dice`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (command match first, flag check second).

Randomness via the stdlib ``random`` module -- see `eightball`'s own
docstring for why no dedicated WIT binding is needed.
"""

from __future__ import annotations

import random
import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-dice"

#: `!dice` alone, or `!dice <spec>` where `<spec>` is validated separately --
#: this only decides "is this bundle's command even being invoked", same
#: division of labor as `bundles/csharp/superpenguin-roll`'s own `ChatCommand`.
COMMAND_PATTERN = re.compile(r"^!dice(?:\s+(?P<spec>\S+))?\s*$", re.IGNORECASE)
#: `NdM` dice-spec syntax, e.g. `2d6`.
DICE_SPEC_PATTERN = re.compile(r"^(?P<count>\d{1,3})[dD](?P<sides>\d{1,4})$")

DEFAULT_COUNT = 1
DEFAULT_SIDES = 6
MIN_COUNT = 1
MAX_COUNT = 100
MIN_SIDES = 2
MAX_SIDES = 1000
MAX_LISTED_ROLLS = 20  # beyond this only the sum is shown, to keep chat replies short

USAGE_TEXT = (
    f"Usage: !dice [NdM] -- e.g. !dice 2d6 "
    f"({MIN_COUNT}-{MAX_COUNT} dice, {MIN_SIDES}-{MAX_SIDES} sides)."
)


def _parse_spec(spec: str | None) -> tuple[int, int] | None:
    """Return `(count, sides)` for a valid spec (or the default), `None` if invalid/out of bounds."""
    if spec is None:
        return DEFAULT_COUNT, DEFAULT_SIDES

    match = DICE_SPEC_PATTERN.match(spec)
    if match is None:
        return None

    count, sides = int(match.group("count")), int(match.group("sides"))
    if not (MIN_COUNT <= count <= MAX_COUNT) or not (MIN_SIDES <= sides <= MAX_SIDES):
        return None
    return count, sides


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!dice [NdM]` -> a dice-roll or usage-error reply.

    Returns `None` for any non-`!dice` payload or while `waddles.command-dice`
    is disabled -- never raises over an event this bundle wasn't meant to
    react to. A recognized-but-malformed/out-of-bounds spec still produces a
    reply (a usage message), since the caller did invoke this command.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    command_match = COMMAND_PATTERN.match(text.strip())
    if command_match is None:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    parsed = _parse_spec(command_match.group("spec"))
    if parsed is None:
        reply_text = USAGE_TEXT
    else:
        count, sides = parsed
        rolls = [random.randint(1, sides) for _ in range(count)]
        if count <= MAX_LISTED_ROLLS:
            reply_text = f"\U0001f3b2 {count}d{sides}: {rolls} (total {sum(rolls)})"
        else:
            reply_text = (
                f"\U0001f3b2 {count}d{sides}: total {sum(rolls)} (rolls omitted)"
            )

    log.info("dice.transform matched", platform=event.platform)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": reply_text, "channel_id": event.payload.get("channel_id")},
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
    """Implement `action-stage.dispatch`: relay the dice reply to the event's own origin platform.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError(
            "dice reply requires a channel_id from the inbound chat.message"
        )

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": payload.get("text", "")})
    log.info("dice.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
