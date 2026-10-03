"""`!lurk`/`!unlurk` -> a per-(community, caller) kv toggle with a TTL, confirmed by relay.

PII note (2026-10-03): the tokenization pipeline (#429) is NOT merged yet, so
`event.actor` may currently be a RAW USERNAME, not an opaque token. This
bundle never stores or logs that raw value -- `_kv_key()` hashes
`(community, actor)` into a non-reversible pseudonym before it ever reaches
`kv`, and no log line below includes `actor`, so no PII enters `kv` or logs
(`client.md` PII Tokenization: "Reference users by UUID ... never a raw
username outside the API server"; the WIT boundary here is exactly such an
outside-the-boundary context). Once #429 lands, `actor` becomes an opaque
token and this same hashing remains correct (and harmless) to keep.

Declares the `storage.kv` permission (`bundle.yaml`/`hub-manifest.yaml`) --
without it, `hub_api/services/bundle_approval_service.py::_derive_
capabilities()` never grants `kv` at all (undeclared means denied, per that
module's own docstring); this is the first Python core bundle to need it.

Business logic split: `transform` only recognizes the command and which
toggle direction it is (no kv access, no side effect) -- `dispatch` performs
the actual kv set/delete AND the relay confirmation, mirroring `pyping`'s own
process/action-stage split where the externally-visible effect lives in the
action stage.

Gated behind the PostHog flag ``waddles.command-lurk`` -- see
`bundles/python/eightball/src/app.py`'s own docstring for the flag-gate
rationale and ordering.
"""

from __future__ import annotations

import hashlib
from typing import Any

from waddle_sdk import kv, log, relay
from waddle_sdk.flask_core.feature_flags import feature_enabled
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

FLAG_KEY = "waddles.command-lurk"

#: 24h -- well under the host's 30-day `KV_MAX_TTL_S` cap (spec kv limits).
#: A lurk toggle is a per-session affordance; if a caller never `!unlurk`s,
#: the entry simply expires rather than accumulating forever.
LURK_TTL_SECONDS = 24 * 60 * 60

LURK_REPLY = "You are now lurking. Use !unlurk to come back."
UNLURK_REPLY = "Welcome back!"

_COMMANDS = {"!lurk": "lurk", "!unlurk": "unlurk"}


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: recognize `!lurk`/`!unlurk`, no kv access here.

    Returns `None` for any non-matching payload or while `waddles.command-lurk`
    is disabled. The resolved `command` ("lurk"/"unlurk") travels in the
    outbound payload for `dispatch` to act on.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    command = _COMMANDS.get(text.strip().lower())
    if command is None:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    log.info("lurk.transform matched", command=command)
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        payload={"command": command, "channel_id": event.payload.get("channel_id")},
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
    return f"lurk:{pseudonym}"


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any
) -> DispatchResult:
    """Implement `action-stage.dispatch`: set/delete the kv toggle, then relay a confirmation.

    Raises:
        ValueError: The envelope's payload has no `channel_id`, or an
            unrecognized `command` (defensive -- `transform` only ever emits
            "lurk"/"unlurk").
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    if not channel_id:
        raise ValueError("lurk reply requires a channel_id from the inbound chat.message")
    command = payload.get("command")
    if command not in ("lurk", "unlurk"):
        raise ValueError(f"unrecognized lurk command: {command!r}")

    key = _kv_key(envelope.community, envelope.event.actor)
    if command == "lurk":
        await kv.set(key, b"1", ttl_seconds=LURK_TTL_SECONDS)
        reply_text = LURK_REPLY
    else:
        await kv.delete(key)
        reply_text = UNLURK_REPLY

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": reply_text})
    log.info("lurk.dispatch relayed", platform=provider, command=command)
    return DispatchResult(transport=provider, detail=command)
