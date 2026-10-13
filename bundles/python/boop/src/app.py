"""`!boop [target]` -> a lighthearted, harmless flavor-text reply, relayed back.

Token-safe (PR batch 2, 2026-10-08): relay only, no kv/db state, never
echoes another user's raw handle in a log line -- safe to ship ahead of the
PII-tokenization pipeline (#427/#429), which means `event.actor` and a
caller-typed `<target>` may currently be raw usernames rather than opaque
tokens. Modeled on `bundles/python/eightball`'s hand-built structure (same
caveat: routed through a hand-authored `_entry_wiring.py`, not
`bundle_compiler`, which is still stubbed) -- structurally twin to
`bundles/python/wave`, distinct flavor bank and no `command_prefix` overlap
with any existing bundle.

All flavor text here is wholesome and harmless by design.

`<target>` shape-check (`_TARGET_RE`, optional leading `@` stripped) is
copied from `bundles/python/duel`/`bundles/python/social`'s own
`_normalize_target()` -- no shared SDK utility exists yet for this shape
check. An unrecognizable target replies with a usage message and touches no
state -- never a silent drop.

No shared-grammar `waddle_sdk.command.CommandSpec`/`parse_command()` use
here, same documented deviation as `eightball`/`wave`: this command's only
argument is an optional free-text target with no verb/sub-module shape.

ALL business logic (target validation + the random pick itself) runs in
`transform()`, mirroring `eightball`/`wheel`'s own documented convention for
a command with no dynamic per-community state to look up -- `dispatch()` is
a pure relay of the text `transform()` already produced.

PII note: the rendered reply text may include the caller-typed target
string (going back to the same public chat channel it came from) -- but no
log line here ever includes the raw target or `event.actor`, only the
resolved command shape (`"solo"` / `"targeted"` / `"usage"`) and platform.

Gated behind the PostHog flag ``waddles.command-boop`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering (cheap command-match first, flag check second, real
classification last).
"""

from __future__ import annotations

import random
import re
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

#: PostHog flag key, `{product}.{feature-name}` convention (`critical-rules.md`).
FLAG_KEY = "waddles.command-boop"

#: A plausible username/mention shape: an optional leading `@` (stripped),
#: then 1-32 chars of letters/digits/underscore/dot/hyphen. Not a lookup
#: against a real roster -- copied from `bundles/python/social`'s own
#: `_USERNAME_RE` (itself copied from `bundles/python/duel`).
_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")

_USAGE = "Usage: !boop [target] -- target must look like a plausible username"

#: Original, comically-harmless flavor text, written fresh for this bundle
#: (module docstring) -- no third-party source or text reused. Used for a
#: bare `!boop` with no target.
_SOLO_BOOPS: tuple[str, ...] = (
    "🐽 Boops the air. Nobody was there, but the intent was pure.",
    "🐽 Practices a precise nose-boop on an imaginary snoot.",
    "🐽 Boops a passing rubber duck. Squeak!",
    "🐽 Extends one finger and boops nothing, dramatically.",
)

#: Used for `!boop <target>` -- `{target}` is `str.format()`-substituted,
#: not `waddle_sdk.command.substitute_placeholders()`'s `$(name)` syntax
#: (same reasoning as `social`'s own `_InteractionSpec.templates`: these are
#: fixed, code-owned strings, not bundle-author-configurable templates).
_TARGETED_BOOPS: tuple[str, ...] = (
    "🐽 {target} gets a gentle boop on the nose!",
    "🐽 Boop! {target} has been snooted.",
    "🐽 A very precise boop lands on {target}'s snoot.",
    "🐽 {target} is booped. Squeak!",
)


def _normalize_target(raw: str) -> str | None:
    """Strip an optional leading `@` and validate the shape; `None` if not a plausible username.

    Shape check only -- not a lookup against a real community roster, same
    documented limitation as `social`/`duel`/`wave`'s own identical helper.
    """
    candidate = raw[1:] if raw.startswith("@") else raw
    if not candidate or not _TARGET_RE.match(candidate):
        return None
    return candidate


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: `!boop [target]` -> a flavor-text reply.

    Returns `None` (no reply, event dropped) for any non-matching payload or
    while the `waddles.command-boop` flag is disabled -- never raises over
    an event this bundle was never meant to react to. A recognized-but-
    malformed `!boop <bad-shape>` still produces a usage reply -- the
    command was invoked, so it is never silently dropped.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() != "!boop":
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    rest = rest.strip()
    if not rest:
        reply_text = random.choice(_SOLO_BOOPS)  # noqa: S311 - a game reply, not a security one
        log.info("boop.transform matched", platform=event.platform, shape="solo")
    else:
        target_token, _, extra = rest.partition(" ")
        target = _normalize_target(target_token) if not extra.strip() else None
        if target is None:
            reply_text = _USAGE
            log.info("boop.transform usage", platform=event.platform, shape="usage")
        else:
            template = random.choice(_TARGETED_BOOPS)  # noqa: S311
            reply_text = template.format(target=target)
            log.info("boop.transform matched", platform=event.platform, shape="targeted")

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
        raise ValueError("boop reply requires a channel_id from the inbound chat.message")

    provider = envelope.event.platform
    text = payload.get("text", "")
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("boop.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
