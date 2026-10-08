"""`!choose a | b | c` (or space-separated options) -> a random pick, relayed to the caller.

Stateless (PR batch 2, 2026-10-08): relay only, no kv/db state -- same shape
as `bundles/python/eightball`'s own module docstring (modeled on
`bundles/python/pyping`'s hand-built structure, routed through a
hand-authored `_entry_wiring.py`, not `bundle_compiler`, which is still
stubbed).

No shared-grammar `waddle_sdk.command.CommandSpec`/`parse_command()` use
here, same documented deviation as `eightball`: this command's only
argument is the caller's own free-text option list, which has no verb/
sub-module shape at all (a first token of e.g. `add` or `list` is just
another option, never a grammar verb) -- forcing it through the shared
verb-based grammar would misparse exactly that case. `eightball`'s own
module docstring documents the same precedent for its own free-text
`<question>` argument.

Option-list grammar: splits on `|` if the caller's text contains one (lets
an option itself contain spaces, e.g. `!choose pizza night | movie night`),
otherwise splits on whitespace (`!choose heads tails`). Fewer than two
non-empty options, or more than `MAX_OPTIONS`, or any option longer than
`MAX_OPTION_LEN` -- all fail-loud with a usage/bounds reply, never silently
truncated or dropped (`critical-rules.md` Fail-Loud Code Paths): the
command was recognized (`!choose` matched), so it always gets a reply.

ALL business logic (parsing + the random pick itself) runs in `transform()`,
mirroring `eightball`/`wheel`'s own documented convention for a command with
no dynamic per-community state to look up -- `dispatch()` is a pure relay of
the text `transform()` already produced.

Token-safe: never logs the caller's own option text or `event.actor`, only
the resolved command name and option count (`choose.transform matched`) --
same PII posture as `eightball`'s own module docstring (ahead of the
tokenization pipeline, #427/#429).

Gated behind the PostHog flag ``waddles.command-choose`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
parse last).
"""

from __future__ import annotations

import random
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention (`critical-rules.md`).
FLAG_KEY = "waddles.command-choose"

#: Bounds on the caller's own option list -- keeps a pathological input
#: (hundreds of options, or a multi-kilobyte single option) from producing
#: an unreasonable reply; neither is a security boundary, just sane chat UX.
MAX_OPTIONS = 20
MAX_OPTION_LEN = 200

_USAGE = "Usage: !choose option1 | option2 | ... (or space-separated: !choose heads tails)"
_TOO_MANY = f"too many options (max {MAX_OPTIONS})"
_TOO_LONG = f"one option is too long (max {MAX_OPTION_LEN} characters)"


def _parse_options(rest: str) -> list[str] | str:
    """Split the caller's own text into options, or return a usage/bounds error string.

    Prefers `|` as the delimiter (lets an option contain spaces); falls back
    to whitespace splitting when no `|` is present. Returns the resolved
    `list[str]` on success, or a ready-to-send error string on any
    usage/bounds violation -- never raises, never returns an empty list.
    """
    if not rest.strip():
        return _USAGE
    if "|" in rest:
        candidates = [part.strip() for part in rest.split("|")]
    else:
        candidates = rest.split()
    options = [opt for opt in candidates if opt]
    if len(options) < 2:
        return _USAGE
    if len(options) > MAX_OPTIONS:
        return _TOO_MANY
    if any(len(opt) > MAX_OPTION_LEN for opt in options):
        return _TOO_LONG
    return options


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!choose ...` -> a random-pick or usage reply.

    Returns `None` (no reply, event dropped) for any non-matching payload or
    while the `waddles.command-choose` flag is disabled -- never raises over
    an event this bundle was never meant to react to. A recognized-but-
    malformed `!choose ...` (too few/many options, one too long) still
    produces a reply -- the command was invoked, so it is never silently
    dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() != "!choose":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    parsed = _parse_options(rest)
    if isinstance(parsed, str):
        reply_text = parsed
        log.info("choose.transform usage", platform=event.platform)
    else:
        reply_text = random.choice(parsed)  # noqa: S311 - a game reply, not a security decision
        log.info("choose.transform matched", platform=event.platform, option_count=len(parsed))

    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"text": reply_text, "channel_id": event.payload.get("channel_id")},
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
    """Implement `action-stage.dispatch`: relay the reply `transform()` already built.

    `config`/`http_client` are accepted but unused -- relays over the WIT
    `relay` host import only, never outbound HTTP.

    Raises:
        ValueError: The envelope's reply payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("choose reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    text = payload.get("text", "")
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("choose.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
