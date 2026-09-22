"""Community chat process bundle -- query and relay chat history.

Ported from `hub_api/services/community_chat.py`'s read-only functions
(`get_chat_history`, `get_chat_channels`) into an App Bundle process stage that
responds to chat query commands (e.g., `!chat-history`, `!channels`).

Returns `None` for non-command messages (no reply). Scopes all DB queries to
the community from `get_bundle_context()` (never from `event.payload`, which
is untrustworthy platform-supplied data). Response text is truncated to fit
typical chat platforms (~4000 chars max).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from flask_core import PlatformEvent, get_bundle_context, get_bundle_dal
from flask_core.bundle_runtime import raw_sql_rows


@dataclass(slots=True, frozen=True)
class ChatMessage:
    """One chat message row from hub_chat_messages."""

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
    """Format a list of ChatMessages into a readable reply string."""
    if not messages:
        return "(no messages found)"

    lines = ["**Chat History (newest first):**"]
    for msg in messages:
        user = msg.sender_username or "unknown"
        ts = msg.created_at[:10] if msg.created_at else "?"
        lines.append(f"[{ts}] {user}: {msg.content[:100]}")

    result = "\n".join(lines[:20])  # max 20 messages in output
    if len(result) > 4000:
        result = result[:3900] + "...(truncated)"
    return result


def _format_channels(channels: list[ChatChannel]) -> str:
    """Format a list of ChatChannels into a readable reply string."""
    if not channels:
        return "(no channels found)"

    lines = ["**Chat Channels:**"]
    for ch in channels:
        lines.append(f"- {ch.name}: {ch.message_count} messages")

    result = "\n".join(lines)
    if len(result) > 4000:
        result = result[:3900] + "...(truncated)"
    return result


async def transform(event: PlatformEvent) -> PlatformEvent | None:
    """Reply to `!chat-history` and `!channels` commands; else no reply.

    Reads `event.payload["text"]` for the command, returns `None` for
    non-command messages or if the command is unrecognized.

    Uses `get_bundle_context()` for tenant/community scope and
    `get_bundle_dal()`/`raw_sql_rows()` (D21a) to query chat history from
    `hub_chat_messages`.

    Raises `ValueError` on a malformed event -- the process runner catches
    this per-event so one bad event never kills the poll loop.
    """
    raw_text = event.payload.get("text")
    if not isinstance(raw_text, str):
        return None
    text = raw_text.strip().lower()

    if text.startswith("!chat-history"):
        try:
            ctx = get_bundle_context()
            dal = get_bundle_dal()

            sql = """
                SELECT id, community_id, channel_name, sender_username, message_content,
                       message_type, created_at
                FROM hub_chat_messages
                WHERE community_id = (
                    SELECT id FROM communities WHERE id = :community_id
                    OR tenant_id = (SELECT id FROM tenants WHERE id = :tenant_id)
                )
                ORDER BY created_at DESC
                LIMIT 20
            """
            rows = await raw_sql_rows(
                dal,
                sql,
                {
                    "community_id": int(ctx.community) if ctx.community else 0,
                    "tenant_id": ctx.tenant,
                },
            )

            messages = [
                ChatMessage(
                    id=row["id"],
                    community_id=row["community_id"],
                    channel_name=row["channel_name"],
                    sender_username=row["sender_username"],
                    content=row["message_content"],
                    message_type=row["message_type"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]
            messages.reverse()  # oldest first in output
            reply_text = _format_chat_history(messages)
        except Exception as e:
            reply_text = f"Error fetching chat history: {str(e)}"

        return dataclasses.replace(
            event,
            payload={**event.payload, "text": reply_text},
        )

    if text.startswith("!channels"):
        try:
            ctx = get_bundle_context()
            dal = get_bundle_dal()

            sql = """
                SELECT channel_name, COUNT(*) AS message_count,
                       MAX(created_at) AS last_message_at
                FROM hub_chat_messages
                WHERE community_id = (
                    SELECT id FROM communities WHERE id = :community_id
                    OR tenant_id = (SELECT id FROM tenants WHERE id = :tenant_id)
                )
                GROUP BY channel_name
                ORDER BY last_message_at DESC
            """
            rows = await raw_sql_rows(
                dal,
                sql,
                {
                    "community_id": int(ctx.community) if ctx.community else 0,
                    "tenant_id": ctx.tenant,
                },
            )

            channels = [
                ChatChannel(
                    name=row["channel_name"] or "general",
                    message_count=int(row["message_count"]),
                    last_message_at=row["last_message_at"],
                )
                for row in rows
            ]
            if not any(c.name == "general" for c in channels):
                channels.insert(
                    0, ChatChannel(name="general", message_count=0, last_message_at=None)
                )
            reply_text = _format_channels(channels)
        except Exception as e:
            reply_text = f"Error fetching channels: {str(e)}"

        return dataclasses.replace(
            event,
            payload={**event.payload, "text": reply_text},
        )

    return None  # no reply for other messages
