"""Chat commands over the community economy service, without a private balance store.

All identity resolution and tool calls run in process-stage, where the host wires
identity and atomic economy capabilities. Action-stage only relays the completed
reply. Admin tools are gated by host-verified community scopes, never chat badges.
"""
from __future__ import annotations

import hashlib
from typing import Any

from waddle_sdk import event_dedupe, identity, log, relay, tool
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SERVICE = "waddles.core.service.economy"
_USAGE = (
    "Usage: !points [view [@user]|leaders|set <amount> @user|add <amount> @user|"
    "sub <amount> @user] | !give <amount> @user"
)
_MAX_AMOUNT = 2**63 - 1


class DispatchResult:
    """The transport result shape consumed by the shared component entry adapter."""

    __slots__ = ("transport", "detail", "sub_type", "http_status")

    def __init__(self, *, transport: str, detail: str) -> None:
        """Record only provider and a non-sensitive outcome code."""
        self.transport = transport
        self.detail = detail
        self.sub_type = None
        self.http_status = None


def _amount(text: str, *, zero: bool = False) -> int:
    """Accept bounded ASCII amounts, rejecting alternate integer syntax."""
    if not text or not text.isascii() or not text.isdigit() or len(text) > 19:
        raise ValueError("invalid amount")
    amount = int(text)
    if not (0 if zero else 1) <= amount <= _MAX_AMOUNT:
        raise ValueError("invalid amount")
    return amount


async def _target(text: str) -> str:
    """Resolve exactly one host-issued mention from the triggering message."""
    tokens = identity.mention_tokens(text)
    if len(tokens) != 1 or text != "{user:" + tokens[0] + "}":
        raise ValueError("target must be a user mention")
    return await identity.resolve_mention(tokens[0])


def _key(event: PlatformEvent) -> str:
    """Use platform message identity for retry safety across process redelivery."""
    message_id = event_dedupe.event_id_from_payload(event.payload, platform=event.platform)
    return hashlib.sha256(f"loyalty:{event.platform}:{message_id}".encode()).hexdigest()


async def _execute(event: PlatformEvent, parts: list[str]) -> str:
    """Resolve the command to one approved service tool and render its result."""
    head, tail = parts[0].lower(), parts[1:]
    if head == "!give":
        if len(tail) != 2:
            raise ValueError("usage")
        amount = _amount(tail[0])
        target = await _target(tail[1])
        actor = await identity.resolve_actor()
        result = await tool.invoke(
            SERVICE, "give", {"from": actor, "to": target, "amount": amount},
            idempotency_key=_key(event),
        )
        return f"Transferred {result['amount']} {result['alias']}."
    if tail and tail[0].lower() in {"leaders", "top"}:
        if len(tail) != 1:
            raise ValueError("usage")
        result = await tool.invoke(SERVICE, "points_leaders", {"limit": 10})
        entries = result["entries"]
        return "Leaderboard (" + result["alias"] + "): " + (
            ", ".join(f"{i}. {row['user']}={row['balance']}" for i, row in enumerate(entries, 1))
            if entries else "no balances yet"
        )
    verb = tail[0].lower() if tail else "view"
    if verb in {"set", "add", "sub"}:
        if len(tail) != 3:
            raise ValueError("usage")
        amount = _amount(tail[1], zero=verb == "set")
        target = await _target(tail[2])
        name = "points_set" if verb == "set" else "points_adjust"
        args = {"user": target, "balance": amount} if verb == "set" else {
            "user": target, "delta": -amount if verb == "sub" else amount,
        }
        result = await tool.invoke(SERVICE, name, args, idempotency_key=_key(event))
    else:
        # Preserve bare !points @mention and !points view @mention.
        target_parts = tail[1:] if verb == "view" and tail else tail
        if len(target_parts) > 1:
            raise ValueError("usage")
        target = await _target(target_parts[0]) if target_parts else await identity.resolve_actor()
        result = await tool.invoke(SERVICE, "points_view", {"user": target})
    return f"Balance: {result['balance']} {result['alias']}."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Perform identity and service work in process-stage; emit a reply for action-stage."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    parts = text.strip().split()
    if not parts or parts[0].lower() not in {"!points", "!give"}:
        return None
    if not event.payload.get("channel_id"):
        raise ValueError("points requires an origin channel")
    try:
        reply = await _execute(event, parts)
    except ValueError:
        reply = _USAGE
    except tool.ToolError as exc:
        log.warn("loyalty.tool_failed", code=exc.code)
        if exc.code not in {"forbidden", "not_granted", "invalid_args"}:
            raise
        reply = "Points request refused (" + exc.code + ")."
    return PlatformEvent(
        platform=event.platform, event_type=event.event_type, actor=event.actor,
        occurred_at=event.occurred_at,
        payload={"channel_id": event.payload["channel_id"], "reply": reply},
    )


async def dispatch(
    envelope: StageEnvelope, config: dict[str, Any], *, http_client: Any,
) -> DispatchResult:
    """Relay the computed result without reading or changing any balance."""
    channel = envelope.event.payload.get("channel_id")
    reply = envelope.event.payload.get("reply")
    if not envelope.community or not isinstance(channel, str) or not channel:
        raise ValueError("points reply requires community and origin channel")
    if not isinstance(reply, str) or not reply:
        raise ValueError("points reply missing")
    await relay.push(envelope.event.platform, {"channel": channel, "text": reply})
    return DispatchResult(transport=envelope.event.platform, detail="points_reply")
