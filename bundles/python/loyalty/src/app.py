"""Chat commands over the community economy service, without a private balance store.

All identity resolution and tool calls run in process-stage, where the host wires
identity and atomic economy capabilities. Action-stage only relays the completed
reply. Admin tools are gated by host-verified community scopes, never chat badges.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from waddle_sdk import event_dedupe, identity, log, relay, tool
from waddle_sdk.command import CliArgument, CliCommandSpec, ParsedCliCommand, parse_cli_command
from waddle_sdk.flask_core.stream_pipeline import PlatformEvent, StageEnvelope

SERVICE = "waddles.core.service.economy"
_TARGET_ARGS = (
    CliArgument("user_value", required=False, help="host-issued user mention"),
    CliArgument("user", ("-u", "--user"), required=False, help="host-issued user mention"),
)
_AMOUNT_ARGS = (
    CliArgument("amount_value", required=False, kind="integer", help="currency amount"),
    CliArgument(
        "amount", ("-a", "--amount"), required=False, kind="integer", help="currency amount"
    ),
)
POINTS_SPEC = CliCommandSpec(
    "points",
    subcommands=(
        CliCommandSpec("view", _TARGET_ARGS, description="Show a community balance"),
        CliCommandSpec("leaders", description="Show the community leaderboard"),
        CliCommandSpec("top", description="Show the community leaderboard"),
        *(
            CliCommandSpec(
                name, _AMOUNT_ARGS + _TARGET_ARGS, description="Admin balance adjustment"
            )
            for name in ("set", "add", "sub")
        ),
    ),
    default_subcommand="view",
)
GIVE_SPEC = CliCommandSpec(
    "give", _AMOUNT_ARGS + _TARGET_ARGS, description="Transfer your currency to a member"
)
_GENERATED_HELP = parse_cli_command("!points --help", POINTS_SPEC).help
if _GENERATED_HELP is None:
    raise RuntimeError("shared command parser did not generate help")
_USAGE: str = _GENERATED_HELP
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


def _amount(value: str | int | None, *, zero: bool = False) -> int:
    """Require an integer in the host's signed money range."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid amount")
    if not (0 if zero else 1) <= value <= _MAX_AMOUNT:
        raise ValueError("invalid amount")
    return value


def _value(parsed: ParsedCliCommand, name: str) -> str | int | None:
    """Combine a flag with its legacy positional alias, refusing ambiguous input."""
    positional = parsed.values.get(name + "_value")
    flagged = parsed.values.get(name)
    if positional is not None and flagged is not None:
        raise ValueError("argument supplied twice")
    return flagged if flagged is not None else positional


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


async def _execute(event: PlatformEvent, parsed: ParsedCliCommand) -> str:
    """Resolve schema-parsed CLI values to one approved service tool."""
    verb = parsed.subcommand or parsed.command
    target_value = _value(parsed, "user")
    if target_value is not None and not isinstance(target_value, str):
        raise ValueError("invalid target")
    if parsed.command == "give":
        amount = _amount(_value(parsed, "amount"))
        if target_value is None:
            raise ValueError("target required")
        target = await _target(target_value)
        actor = await identity.resolve_actor()
        result = await tool.invoke(
            SERVICE,
            "give",
            {"from": actor, "to": target, "amount": amount},
            idempotency_key=_key(event),
        )
        return f"Transferred {result['amount']} {result['alias']}."
    if verb in {"leaders", "top"}:
        result = await tool.invoke(SERVICE, "points_leaders", {"limit": 10})
        entries = result["entries"]
        return f"Leaderboard ({result['alias']}): " + (
            ", ".join(f"{i}. {row['user']}={row['balance']}" for i, row in enumerate(entries, 1))
            if entries
            else "no balances yet"
        )
    if verb in {"set", "add", "sub"}:
        amount = _amount(_value(parsed, "amount"), zero=verb == "set")
        if target_value is None:
            raise ValueError("target required")
        target = await _target(target_value)
        name = "points_set" if verb == "set" else "points_adjust"
        args = (
            {"user": target, "balance": amount}
            if verb == "set"
            else {
                "user": target,
                "delta": -amount if verb == "sub" else amount,
            }
        )
        result = await tool.invoke(SERVICE, name, args, idempotency_key=_key(event))
    else:
        target = await _target(target_value) if target_value else await identity.resolve_actor()
        result = await tool.invoke(SERVICE, "points_view", {"user": target})
    return f"Balance: {result['balance']} {result['alias']}."


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Perform identity and service work in process-stage; emit a reply for action-stage."""
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    command = re.match(r"^\s*!(points|give)(?=\s|$)", text, re.IGNORECASE)
    if command is None:
        return None
    if not event.payload.get("channel_id"):
        raise ValueError("points requires an origin channel")
    try:
        parsed = parse_cli_command(text, GIVE_SPEC if command[1].lower() == "give" else POINTS_SPEC)
        reply = parsed.help if parsed.help is not None else await _execute(event, parsed)
    except ValueError:
        reply = _USAGE
    except tool.ToolError as exc:
        log.warn("loyalty.tool_failed", code=exc.code)
        if exc.code not in {"forbidden", "not_granted", "invalid_args"}:
            raise
        reply = "Points request refused (" + exc.code + ")."
    return PlatformEvent(
        platform=event.platform,
        event_type=event.event_type,
        actor=event.actor,
        occurred_at=event.occurred_at,
        payload={"channel_id": event.payload["channel_id"], "reply": reply},
    )


async def dispatch(
    envelope: StageEnvelope,
    config: dict[str, Any],
    *,
    http_client: Any,
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
