"""`!chat-history [list]` / `!channels [list]` -- read-only chat history queries.

Ported from `core/svc_process/bundles/community_chat_process.py`'s two
read-only commands into a WASM-component App Bundle. Behavior parity with
the source, with two deliberate changes:

1. **Grammar**: parsing goes through the standard `waddle_sdk.command`
   grammar (`parse_command`/`CommandSpec`, SDK #618) instead of hand-rolled
   `text.startswith(...)` -- `!chat-history` and `!chat-history list` are
   equivalent (both show history); `!channels` and `!channels list` are
   equivalent (both list channels). Neither command declares sub-modules
   (there is nothing to enable/disable here), so `waddle_sdk.sub_modules.
   SubModuleGate` is not used -- see that module's own docstring for when
   it would apply.
2. **Scope**: community-scoped only (`community_id`), never tenant --
   the source's `communities`/`tenants` join subquery is dropped. The SDK
   `waddle_sdk.db` facade (spec D21) has no live SQLAlchemy connection to
   express that join on anyway (only a single-table query builder plus a
   raw `execute()` escape hatch for one parameterized statement at a
   time -- `raw_sql_rows`/`raw_sql_write` are explicitly `NotImplemented`
   in this facade), and the task's own scope decision is community-only.
   Likewise, `GROUP BY`/`ORDER BY`/`LIMIT` aren't expressible in the
   facade's query builder (`AsyncQuerySet.select()` raises on `orderby`/
   `limitby`) -- both commands below fetch every matching row for the
   community with a single `WHERE community_id = $1` and sort/aggregate/
   truncate in guest Python instead.

Read-only -- open to anyone, matching the source (no `_is_privileged()`
gate, unlike `count`/`lurk`'s mutating commands).

PII note: chat messages carry `sender_username`. Reply text includes it
(same as the source -- that's the whole point of a chat-history read),
but every `log.*` call here is scoped to command/error metadata only,
never message content or usernames (AUTHORING.md's log-sanitization rule;
see also critical-rules.md PII Tokenization).

Gated behind the PostHog flag ``waddles.command-chat``, default OFF (see
`bundles/python/count/src/app.py`'s own docstring for the flag-gate
rationale and ordering: cheapest disqualifying checks first, flag check
only once a `!`-prefixed, command-matching message is in hand).

**BUILD-ONLY / INERT**: `core/svc_process`'s `bot_process._FEATURE_MODULES`
still runs the original `community_chat_process` monolith module -- this
bundle is not yet the live implementation. Cut-over is a separate, later
change (P4 activation gate). Shipped flag-OFF and unactivated.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from waddle_sdk import log, relay
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command
from waddle_sdk.db import DALError
from waddle_sdk.flask_core import (
    BundleContext,
    PlatformEvent,
    StageEnvelope,
    get_bundle_context,
    get_bundle_dal,
)
from waddle_sdk.flask_core.feature_flags import feature_enabled

FLAG_KEY = "waddles.command-chat"

_CHAT_HISTORY_SPEC = CommandSpec(name="chat-history")
_CHANNELS_SPEC = CommandSpec(name="channels")

#: Max messages shown by `!chat-history` -- matches the source's `LIMIT 20`.
_MAX_HISTORY_MESSAGES = 20

#: Max reply length -- matches the source's ~4000-char chat-platform budget.
_MAX_REPLY_CHARS = 4000
_TRUNCATE_AT = 3900

_NO_COMMUNITY_REPLY = (
    "Chat history isn't available outside a community channel."
)


@dataclass(slots=True, frozen=True)
class ChatMessage:
    """One chat message row from `hub_chat_messages`."""

    id: int
    community_id: int
    channel_name: str | None
    sender_username: str | None
    content: str
    message_type: str
    created_at: str | None


@dataclass(slots=True, frozen=True)
class ChatChannel:
    """One distinct chat channel with aggregate activity."""

    name: str
    message_count: int
    last_message_at: str | None


def _format_chat_history(messages: list[ChatMessage]) -> str:
    """Format a list of `ChatMessage`s into a readable reply string, newest-command-first input.

    `messages` is expected oldest-first (the caller already sorted/sliced)
    -- matches `community_chat_process._format_chat_history`'s contract.
    """
    if not messages:
        return "(no messages found)"

    lines = ["**Chat History (newest first):**"]
    for msg in messages:
        user = msg.sender_username or "unknown"
        ts = msg.created_at[:10] if msg.created_at else "?"
        lines.append(f"[{ts}] {user}: {msg.content[:100]}")

    # `lines[:_MAX_HISTORY_MESSAGES]` matches the source's own `lines[:20]` exactly, including
    # its quirk: `lines` is the header plus up to `_MAX_HISTORY_MESSAGES` message lines, so this
    # slice silently drops the oldest message line when the input is a full 20 messages (the
    # header itself consumes one of the 20 slots). Reproduced as-is for behavior parity, not
    # fixed here -- a real fix is a source-bundle change, out of this port's scope.
    result = "\n".join(lines[:_MAX_HISTORY_MESSAGES])
    if len(result) > _MAX_REPLY_CHARS:
        result = result[:_TRUNCATE_AT] + "...(truncated)"
    return result


def _format_channels(channels: list[ChatChannel]) -> str:
    """Format a list of `ChatChannel`s into a readable reply string."""
    if not channels:
        return "(no channels found)"

    lines = ["**Chat Channels:**"]
    for ch in channels:
        lines.append(f"- {ch.name}: {ch.message_count} messages")

    result = "\n".join(lines)
    if len(result) > _MAX_REPLY_CHARS:
        result = result[:_TRUNCATE_AT] + "...(truncated)"
    return result


def _resolve_community_id(ctx: BundleContext) -> int | None:
    """Parse `ctx.community` into the `community_id` int this bundle's queries need.

    Returns `None` if no community is bound (e.g. a DM/non-community
    context) or the bound value isn't a valid integer -- never guesses or
    defaults to `0` (the source's own `int(ctx.community) if ctx.community
    else 0` is a documented deviation this bundle deliberately does not
    repeat: a "no community" state is a distinct, visible reply, not a
    silent query against community_id=0).
    """
    if ctx.community is None:
        return None
    try:
        return int(ctx.community)
    except ValueError as exc:
        # Expected-input skip, not a fault -- some transports bind a non-numeric
        # `ctx.community` (e.g. a slug). Never log the raw value (PII/platform-ID
        # boundary); only the fact that parsing was skipped and why.
        log.debug("chat.community_id_unparseable", error_type=type(exc).__name__)
        return None


async def _fetch_chat_history(dal: Any, community_id: int) -> list[ChatMessage]:
    """Fetch the community's most recent `_MAX_HISTORY_MESSAGES`, returned oldest-first.

    Issues one `SELECT * FROM hub_chat_messages WHERE community_id = $1`
    (the facade's query builder has no `ORDER BY`/`LIMIT` -- see module
    docstring) and sorts/slices in guest Python.

    Raises:
        DALError: The `db` host call failed.
    """
    rows = await dal(dal.hub_chat_messages.community_id == community_id).select()
    messages = [
        ChatMessage(
            id=int(row["id"]),
            community_id=int(row["community_id"]),
            channel_name=row.get("channel_name"),
            sender_username=row.get("sender_username"),
            content=str(row["message_content"]),
            message_type=str(row["message_type"]),
            created_at=row.get("created_at"),
        )
        for row in rows
    ]
    messages.sort(key=lambda m: m.created_at or "", reverse=True)
    newest_first = messages[:_MAX_HISTORY_MESSAGES]
    newest_first.reverse()  # oldest first in output, matches the source
    return newest_first


async def _fetch_channels(dal: Any, community_id: int) -> list[ChatChannel]:
    """Fetch every distinct channel for the community with its message count and last activity.

    Same single-`WHERE`-clause fetch as :func:`_fetch_chat_history`;
    `GROUP BY`/aggregate is done in guest Python since the facade's query
    builder has no `GROUP BY`. Always includes a `general` entry (even
    `message_count=0`) when the community has never posted there, matching
    the source's sentinel-channel behavior.

    Raises:
        DALError: The `db` host call failed.
    """
    rows = await dal(dal.hub_chat_messages.community_id == community_id).select()
    aggregates: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("channel_name") or "general"
        created_at = row.get("created_at")
        entry = aggregates.setdefault(name, {"count": 0, "last": None})
        entry["count"] += 1
        if created_at is not None and (entry["last"] is None or created_at > entry["last"]):
            entry["last"] = created_at

    channels = [
        ChatChannel(name=name, message_count=data["count"], last_message_at=data["last"])
        for name, data in aggregates.items()
    ]
    channels.sort(key=lambda c: c.last_message_at or "", reverse=True)
    if not any(c.name == "general" for c in channels):
        channels.insert(0, ChatChannel(name="general", message_count=0, last_message_at=None))
    return channels


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Implement `process-stage.transform`: reply to `!chat-history`/`!channels`, else `None`.

    Returns `None` for any non-chat payload, text with no leading `!`
    (cheap-skip before the flag check), an unrecognized leading command
    token, and while `waddles.command-chat` is disabled. Once a command
    token matches, parsing/db failures always produce a reply -- never a
    silent drop (AUTHORING.md's fail-loud rule) -- while an exception from
    `get_bundle_context()`/`get_bundle_dal()` (unbound runtime) is left to
    propagate, since that is a host wiring bug, not a per-message
    condition this bundle can usefully recover from.
    """
    text = event.payload.get("text")
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None

    head = stripped.split(maxsplit=1)[0].lower()
    if head == f"!{_CHAT_HISTORY_SPEC.name}":
        spec = _CHAT_HISTORY_SPEC
    elif head == f"!{_CHANNELS_SPEC.name}":
        spec = _CHANNELS_SPEC
    else:
        return None

    if not await feature_enabled(FLAG_KEY, default=False):
        return None

    try:
        parsed = parse_command(stripped, spec)
    except CommandUsageError as exc:
        reply_text = str(exc)
    else:
        if parsed.option not in (None, "list"):
            reply_text = f"Usage: !{spec.name} [list]"
        else:
            ctx = get_bundle_context()
            community_id = _resolve_community_id(ctx)
            if community_id is None:
                reply_text = _NO_COMMUNITY_REPLY
            else:
                dal = get_bundle_dal()
                try:
                    if spec is _CHAT_HISTORY_SPEC:
                        messages = await _fetch_chat_history(dal, community_id)
                        reply_text = _format_chat_history(messages)
                    else:
                        channels = await _fetch_channels(dal, community_id)
                        reply_text = _format_channels(channels)
                except DALError as exc:
                    log.error("chat.db_failure", command=spec.name, error=str(exc))
                    reply_text = (
                        "Something went wrong fetching chat data - please try again."
                    )

    log.info("chat.transform matched", command=spec.name)
    return dataclasses.replace(
        event,
        payload={**event.payload, "text": reply_text},
    )


class DispatchResult:
    """`waddle_transports.TransportResult`-shaped result -- see `count`'s own `app.py`."""

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
    """Implement `action-stage.dispatch`: relay the reply text `transform` already built.

    Pure relay, same minimal shape as `pyping`/`count`'s own `dispatch` --
    all query/formatting work happens in `transform`.

    Raises:
        ValueError: The envelope's payload is missing `channel_id` or
            `text` (defensive -- `transform` always sets both when it
            returns a non-`None` event).
    """
    payload = envelope.event.payload
    channel_id = payload.get("channel_id")
    text = payload.get("text")
    if not channel_id:
        raise ValueError("chat reply requires a channel_id from the inbound chat.message")
    if not isinstance(text, str) or not text:
        raise ValueError("chat reply requires text produced by transform")

    provider = envelope.event.platform
    await relay.push(provider, {"channel": channel_id, "text": text})
    log.info("chat.dispatch relayed", platform=provider)
    return DispatchResult(transport=provider, detail="relayed")
