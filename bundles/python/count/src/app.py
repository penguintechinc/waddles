"""`!count` -> a per-(community, caller) kv-backed self counter, incremented and relayed.

PII note (2026-10-03): the tokenization pipeline (#429) is NOT merged yet, so
`event.actor` may currently be a RAW USERNAME, not an opaque token. This
bundle never stores or logs that raw value -- `_kv_key()` hashes
`(community, actor)` into a non-reversible pseudonym before it ever reaches
`kv`, and no log line below includes `actor`. See
`bundles/python/lurk/src/app.py`'s own docstring for the same rationale,
shared verbatim here. Once #429 lands, `actor` becomes an opaque token and
this same hashing remains correct (and harmless) to keep.

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

import hashlib
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

    log.info("count.transform matched")
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
    """Per-(community, caller) key, hashed so a raw `actor` username is never stored in `kv`.

    `actor` may still be a raw username (tokenization pipeline #429 not yet
    merged) -- SHA-256 the `(community, actor)` pair into a non-reversible
    pseudonym so the stored key never contains PII, today or after #429.
    """
    pseudonym = hashlib.sha256(
        f"{community or 'tenant'}:{actor or 'anonymous'}".encode()
    ).hexdigest()
    return f"count:{pseudonym}"


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
