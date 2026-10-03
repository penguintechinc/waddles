"""`!count` -> a per-(community, caller) kv-backed self counter, incremented and relayed.

Token-safe (PR batch 1, 2026-10-03): the kv key is built from the envelope's
own opaque `actor`/`community` fields, never a raw username -- see
`bundles/python/lurk/src/app.py`'s own docstring for the same PII-tokenization
rationale, shared verbatim here.

Declares the `storage.kv` permission -- see `lurk`'s own docstring for why
it must be present in both `bundle.yaml` and `hub-manifest.yaml`.

Business logic split: `transform` only recognizes the command (no kv
access) -- `dispatch` performs the actual `kv.increment()` AND builds the
reply text from its result, since the new total isn't known until the
increment happens; this is the one respect in which `count` differs from
`lurk`'s split (lurk's reply text is static per direction, decidable in
`transform`; count's reply text depends on `dispatch`'s own kv round trip).

Gated behind the PostHog flag ``waddles.command-count`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering.
"""

from __future__ import annotations

from typing import Any

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-count"

#: `!count` alone -- no arguments, no kv access here (see module docstring).
COMMAND = "!count"


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!count`, no kv access here.

    Returns `None` for any non-matching payload or while `waddles.command-count`
    is disabled.
    """
    text = event.payload.get("text")
    if not isinstance(text, str) or text.strip().lower() != COMMAND:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    log.info("count.transform matched", actor=event.actor)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"channel_id": event.payload.get("channel_id")},
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


def _kv_key(community: str | None, actor: str | None) -> str:
    """Per-(community, caller) key, scoped by the envelope's own opaque fields, never a username."""
    return f"count:{community or 'tenant'}:{actor or 'anonymous'}"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: increment the kv counter, then relay the new total.

    `ttl_seconds=0` (no expiry, `wit/waddle-bundle/stage.wit`'s own kv docstring) --
    a running self-counter is meant to persist indefinitely, unlike `lurk`'s
    session-scoped toggle.

    Raises:
        ValueError: The envelope's payload has no `channel_id`.
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("count reply requires a channel_id from the inbound chat.message")

    key = _kv_key(envelope.community, envelope.event.actor)
    total = await kv.increment(key, 1, ttl_seconds=0)
    plural = "" if total == 1 else "s"
    reply_text = f"You've been counted {total} time{plural}!"

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("count.dispatch relayed", platform=provider, total=total)
    return DispatchResult(transport=provider, detail=f"total={total}")
