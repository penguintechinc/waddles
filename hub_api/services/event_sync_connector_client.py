"""Connector-path Discord scheduled-event client (event sync via svc-action -> svc-ingest).

hub-api holds no Discord credential and no outbound ``pin_sig`` key. This client
implements the engine's ``DiscordEventTargetClient`` seam by enqueueing a SIGNED
request on the Valkey list svc-action consumes, then popping the result svc-action
posts once svc-ingest's executor has confirmed (or refused) the Discord call:

    hub-api --LPUSH signed request--> waddles:event-sync:discord:requests
    hub-api <--BLPOP result--------- waddles:event-sync:discord:result:<request_id>

The request MAC is byte-compatible with ``core/svc_action/src/pin_sig.rs`` (HMAC-SHA256,
length-prefixed fields under a dedicated key ring ``EVENT_SYNC_REQUEST_KEYS`` -- never the
outbound ring) so an unauthenticated Valkey writer cannot make the platform create, edit
or cancel a guild event. The guild is never chosen here: ``expect_guild_id`` is only an
assertion the host compares to the community's binding. Fail loud: every failure raises
``DiscordEventSyncError`` -- nothing falls back to a direct Discord call.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import time
import uuid
from collections.abc import Callable
from hashlib import sha256
from typing import Any

logger = logging.getLogger(__name__)

REQUEST_QUEUE = "waddles:event-sync:discord:requests"
RESULT_PREFIX = "waddles:event-sync:discord:result:"
#: Domain-separation label of the request MAC (``REQUEST_MAC_PLATFORM`` in svc-action).
_MAC_PLATFORM = "event-sync-request"
_MAC_LABEL = b"waddles.outbound-pin.v2"
_MIN_KEY_BYTES = 16
_REQUEST_TTL_S = 120
_DEFAULT_RESULT_TIMEOUT_S = 15.0
#: The host's stable ack code for a Discord 404 (the event was deleted out of band).
NOT_FOUND_CODE = "upstream_not_found"


class RequestKeyError(Exception):
    """The event-sync request key ring is missing or malformed."""


def parse_key_ring(raw: str, active_kid: str | None) -> tuple[dict[str, bytes], str]:
    """Parse ``kid:hexkey[,...]`` and select the active signing kid.

    Raises ``RequestKeyError`` for an empty/malformed ring, a key under 16 bytes, or an
    active kid that is not in the ring.
    """
    keys: dict[str, bytes] = {}
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        kid, sep, hexkey = entry.partition(":")
        try:
            key = bytes.fromhex(hexkey)
        except ValueError as exc:
            raise RequestKeyError("malformed event-sync request key entry") from exc
        if not sep or not kid or len(key) < _MIN_KEY_BYTES:
            raise RequestKeyError("malformed event-sync request key entry")
        keys[kid] = key
    if not keys:
        raise RequestKeyError("no event-sync request keys configured")
    if not active_kid or active_kid not in keys:
        raise RequestKeyError("no active event-sync request signing key")
    return keys, active_kid


def keys_from_env() -> tuple[dict[str, bytes], str]:
    """Load the signing ring from ``EVENT_SYNC_REQUEST_KEYS`` / ``..._ACTIVE_KID``."""
    raw = os.environ.get("EVENT_SYNC_REQUEST_KEYS", "")
    if not raw:
        raise RequestKeyError("EVENT_SYNC_REQUEST_KEYS is not set")
    return parse_key_ring(raw, os.environ.get("EVENT_SYNC_REQUEST_ACTIVE_KID"))


def sign_request(
    key: bytes,
    kid: str,
    *,
    action: str,
    expect_guild_id: str,
    discord_event_id: str,
    event_json: str,
    request_id: str,
    exp_ms: int,
    community_id: int,
    tenant_id: int,
) -> str:
    """``kid:hexmac`` over the request fields -- identical to ``PinKeys::sign`` in svc-action."""
    mac = hmac.new(key, _MAC_LABEL, sha256)
    parts = [
        _MAC_PLATFORM,
        action,
        "",  # channel
        expect_guild_id,  # origin_channel
        discord_event_id,  # message_id
        event_json,  # text
        "",  # user_id
        request_id,  # op_id
        str(exp_ms),
        str(community_id),
        str(tenant_id),
    ]
    for part in parts:
        raw = part.encode("utf-8")
        mac.update(len(raw).to_bytes(4, "big"))
        mac.update(raw)
    return f"{kid}:{mac.hexdigest()}"


def _event_json(payload: Any) -> str:
    """The flat event field object the host validates (never the Discord REST body)."""
    fields: dict[str, Any] = {
        "name": payload.name,
        "scheduled_start_time": payload.scheduled_start_time,
        "scheduled_end_time": payload.scheduled_end_time,
        "location": payload.location,
    }
    if payload.description:
        fields["description"] = payload.description
    return json.dumps(fields, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


class ConnectorEventClient:
    """``DiscordEventTargetClient`` over the signed Valkey request/result handshake."""

    def __init__(
        self,
        valkey: Any,
        *,
        tenant_id: int,
        community_id: int,
        keys: tuple[dict[str, bytes], str],
        error_cls: type[Exception],
        not_found_cls: type[Exception],
        result_timeout_s: float = _DEFAULT_RESULT_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind to one community scope; ``error_cls``/``not_found_cls`` are the engine errors."""
        self._valkey = valkey
        self._tenant_id = tenant_id
        self._community_id = community_id
        self._ring, self._kid = keys
        self._error = error_cls
        self._not_found = not_found_cls
        self._timeout_s = result_timeout_s
        self._clock = clock

    async def _roundtrip(
        self, action: str, *, guild_id: str, event_id: str = "", event_json: str = ""
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        exp_ms = int(self._clock() * 1000) + _REQUEST_TTL_S * 1000
        sig = sign_request(
            self._ring[self._kid],
            self._kid,
            action=action,
            expect_guild_id=guild_id,
            discord_event_id=event_id,
            event_json=event_json,
            request_id=request_id,
            exp_ms=exp_ms,
            community_id=self._community_id,
            tenant_id=self._tenant_id,
        )
        request = {
            "v": 1,
            "request_id": request_id,
            "tenant_id": self._tenant_id,
            "community_id": self._community_id,
            "action": action,
            "discord_event_id": event_id or None,
            "expect_guild_id": guild_id,
            "event_json": event_json or None,
            "exp_ms": exp_ms,
            "sig": sig,
        }
        try:
            await self._valkey.lpush(REQUEST_QUEUE, json.dumps(request))
            popped = await self._valkey.blpop(
                f"{RESULT_PREFIX}{request_id}", timeout=max(1, math.ceil(self._timeout_s))
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced loudly as an engine error
            logger.error(
                "event_sync_connector.valkey_failed action=%s error_type=%s",
                action,
                type(exc).__name__,
            )
            raise self._error(f"event sync {action}: valkey unavailable") from exc
        if popped is None:
            logger.error("event_sync_connector.timeout action=%s", action)
            raise self._error(
                f"event sync {action} was not confirmed in time (svc-action consumer down?)"
            )
        raw = popped[1]
        try:
            result = json.loads(raw)
            ok = result["ok"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("event_sync_connector.unreadable_result action=%s", action)
            raise self._error(f"event sync {action}: unreadable result") from exc
        if ok is not True:
            code = str(result.get("code", "failed"))
            if code == NOT_FOUND_CODE:
                raise self._not_found(f"discord scheduled event not found (404) during {action}")
            logger.warning("event_sync_connector.refused action=%s code=%s", action, code)
            raise self._error(f"event sync {action} refused: {code}")
        return dict(result)

    async def create_scheduled_event(self, *, guild_id: str, payload: Any) -> str:
        """Create; returns the new Discord scheduled-event id reported by the host."""
        result = await self._roundtrip("create", guild_id=guild_id, event_json=_event_json(payload))
        event_id = result.get("id")
        if not isinstance(event_id, str) or not event_id.isdigit():
            raise self._error("event sync create: no event id in the confirmed result")
        return event_id

    async def patch_scheduled_event(
        self, *, guild_id: str, discord_event_id: str, payload: Any
    ) -> None:
        """Update with the FULL field set (never a diff)."""
        await self._roundtrip(
            "update", guild_id=guild_id, event_id=discord_event_id, event_json=_event_json(payload)
        )

    async def cancel_scheduled_event(self, *, guild_id: str, discord_event_id: str) -> None:
        """Cancel (status=CANCELED, history preserved) -- never the DELETE verb."""
        await self._roundtrip("cancel", guild_id=guild_id, event_id=discord_event_id)
