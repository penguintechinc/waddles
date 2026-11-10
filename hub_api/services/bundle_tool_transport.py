"""Signed Valkey request/result transport for hub-api -> bundle-tool invocation.

hub-api never loads or runs a bundle, and the host exposes no HTTP surface for tool calls (a
bundle tool only runs inside the executor, reached from a guest's `tool.invoke`). The generic
bundle-admin gateway therefore hands a SIGNED request to the host the same way event sync does
(`event_sync_connector_client`):

    hub-api --LPUSH signed request--> waddles:bundle-admin:requests
    hub-api <--BLPOP result---------- waddles:bundle-admin:result:<request_id>

The host (svc-action consumer -- see docs/BUNDLE_ADMIN_GATEWAY.md "Needs from S0") verifies the
HMAC, applies the same P1 matrix / `tool.call` gate / `required_scope` checks a guest `tool.invoke`
gets, runs the target's `tool-provider.call`, and posts `{ok, result | code}` to the result key.
The MAC covers every routing and identity field, including the hub-api-verified `actor_scope`, so
an unauthenticated Valkey writer cannot invoke a tool or claim an admin role. Domain-separated
from the event-sync and outbound-pin rings (own key ring `BUNDLE_ADMIN_REQUEST_KEYS`).

Fail loud: every failure raises :class:`ToolTransportError` -- nothing falls back to a direct call.
PII-free: logs carry ids, tool name and a stable code, never `args` or `result`.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

from services import valkey_admin_client
from services.event_sync_connector_client import RequestKeyError, parse_key_ring

logger = logging.getLogger(__name__)

REQUEST_QUEUE = "waddles:bundle-admin:requests"
RESULT_PREFIX = "waddles:bundle-admin:result:"
_MAC_PLATFORM = "bundle-admin-request"
_MAC_LABEL = b"waddles.bundle-admin.v1"
_REQUEST_TTL_S = 120
DEFAULT_RESULT_TIMEOUT_S = 15.0
#: Largest tool result the gateway will accept from the host (bytes of JSON).
MAX_RESULT_BYTES = 1024 * 1024
_CODE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


class ToolTransportError(Exception):
    """A tool call failed in transport or on the host; `status`/`code` map onto the HTTP reply."""

    def __init__(self, status: int, code: str, message: str) -> None:
        """Keep the HTTP `status`, stable machine `code` and a PII-free `message`."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(slots=True, frozen=True)
class ToolCall:
    """One hub-api-authorised tool invocation (everything the MAC covers)."""

    tenant_id: int
    community_id: int
    actor_user_id: int
    actor_scope: str
    caller_app_id: str
    target_app_id: str
    tool: str
    args_json: str


class ToolTransport(Protocol):
    """Anything that can run a :class:`ToolCall` on the host and return the tool's result."""

    async def invoke(self, call: ToolCall) -> dict[str, Any]:
        """Run `call`; return the tool output object or raise `ToolTransportError`."""
        ...


def canonical_args(args: dict[str, Any]) -> str:
    """Deterministic JSON for `args` -- the exact bytes the MAC signs and the host verifies."""
    return json.dumps(args, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def sign_call(key: bytes, kid: str, call: ToolCall, *, request_id: str, exp_ms: int) -> str:
    """``kid:hexmac`` over every call field -- length-prefixed, domain-separated."""
    mac = hmac.new(key, _MAC_LABEL, sha256)
    parts = [
        _MAC_PLATFORM,
        call.target_app_id,
        call.tool,
        call.caller_app_id,
        call.args_json,
        request_id,
        str(exp_ms),
        str(call.community_id),
        str(call.tenant_id),
        str(call.actor_user_id),
        call.actor_scope,
    ]
    for part in parts:
        raw = part.encode("utf-8")
        mac.update(len(raw).to_bytes(4, "big"))
        mac.update(raw)
    return f"{kid}:{mac.hexdigest()}"


def keys_from_env() -> tuple[dict[str, bytes], str]:
    """Load the signing ring from ``BUNDLE_ADMIN_REQUEST_KEYS`` / ``..._ACTIVE_KID``.

    Raises:
        ToolTransportError: 503 `bundle_tool_transport_unconfigured` when absent or malformed.
    """
    raw = os.environ.get("BUNDLE_ADMIN_REQUEST_KEYS", "")
    try:
        if not raw:
            raise RequestKeyError("not set")
        return parse_key_ring(raw, os.environ.get("BUNDLE_ADMIN_REQUEST_ACTIVE_KID"))
    except RequestKeyError as exc:
        logger.error("bundle_admin.transport_unconfigured")
        raise ToolTransportError(
            503,
            "bundle_tool_transport_unconfigured",
            "bundle tool signing keys are not configured",
        ) from exc


class ValkeyToolTransport:
    """:class:`ToolTransport` over the signed Valkey request/result handshake."""

    def __init__(
        self,
        valkey_factory: Callable[[], Any] | None = None,
        *,
        keys: tuple[dict[str, bytes], str] | None = None,
        result_timeout_s: float = DEFAULT_RESULT_TIMEOUT_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind a Valkey client factory and signing ring (env-loaded lazily when omitted)."""
        self._valkey_factory = valkey_factory or valkey_admin_client.build_client
        self._keys = keys
        self._timeout_s = result_timeout_s
        self._clock = clock

    async def invoke(self, call: ToolCall) -> dict[str, Any]:
        """Sign, enqueue and await one call; every failure is a typed `ToolTransportError`."""
        ring, kid = self._keys if self._keys is not None else keys_from_env()
        request_id = str(uuid.uuid4())
        exp_ms = int(self._clock() * 1000) + _REQUEST_TTL_S * 1000
        request = {
            "v": 1,
            "request_id": request_id,
            "tenant_id": call.tenant_id,
            "community_id": call.community_id,
            "actor_user_id": call.actor_user_id,
            "actor_scope": call.actor_scope,
            "caller_app_id": call.caller_app_id,
            "target_app_id": call.target_app_id,
            "tool": call.tool,
            "args_json": call.args_json,
            "exp_ms": exp_ms,
            "sig": sign_call(ring[kid], kid, call, request_id=request_id, exp_ms=exp_ms),
        }
        valkey = self._valkey_factory()
        try:
            await valkey.lpush(REQUEST_QUEUE, json.dumps(request))
            popped = await valkey.blpop(
                f"{RESULT_PREFIX}{request_id}", timeout=max(1, math.ceil(self._timeout_s))
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced loudly as a typed 503
            logger.error(
                "bundle_admin.valkey_failed app_id=%s tool=%s error_type=%s",
                call.target_app_id,
                call.tool,
                type(exc).__name__,
            )
            raise ToolTransportError(
                503, "bundle_tool_transport_unavailable", "bundle tool transport unavailable"
            ) from exc
        finally:
            await valkey.aclose()
        if popped is None:
            logger.error("bundle_admin.timeout app_id=%s tool=%s", call.target_app_id, call.tool)
            raise ToolTransportError(
                504, "bundle_tool_timeout", "the bundle tool did not answer in time"
            )
        return _parse_result(popped[1], call)


def _parse_result(raw: Any, call: ToolCall) -> dict[str, Any]:
    """Decode the host's `{ok, result | code}` reply; refuse anything malformed or oversized."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes | bytearray) else str(raw)
    if len(text) > MAX_RESULT_BYTES:
        logger.error(
            "bundle_admin.result_too_large app_id=%s tool=%s", call.target_app_id, call.tool
        )
        raise ToolTransportError(
            502, "bundle_tool_result_too_large", "bundle tool result too large"
        )
    try:
        reply = json.loads(text)
        ok = reply["ok"]
    except (ValueError, KeyError, TypeError) as exc:
        logger.error(
            "bundle_admin.unreadable_result app_id=%s tool=%s", call.target_app_id, call.tool
        )
        raise ToolTransportError(
            502, "bundle_tool_bad_reply", "unreadable reply from the bundle host"
        ) from exc
    if ok is True:
        result = reply.get("result")
        if not isinstance(result, dict):
            raise ToolTransportError(
                502, "bundle_tool_bad_reply", "the bundle host reply carried no result object"
            )
        return result
    code = reply.get("code") if isinstance(reply, dict) else None
    safe_code = code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "failed"
    logger.warning(
        "bundle_admin.refused app_id=%s tool=%s code=%s", call.target_app_id, call.tool, safe_code
    )
    raise ToolTransportError(
        422 if reply.get("tool_error") is True else 502,
        f"bundle_tool_{safe_code}",
        f"the bundle tool call was refused ({safe_code})",
    )
