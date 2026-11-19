"""Chat service -- REST-only port of Node's `chatController.js`.

Ports `getChatHistory`/`getChatChannels` (both pure reads against
`hub_chat_messages`). The realtime leg (`websocket/index.js` +
`websocket/chatHandler.js`, Socket.io `chat:join/leave/message/typing/
history`) is deliberately **not** ported here -- see
`blueprints/v1/community_chat.py`'s module docstring for why (mounting
`python-socketio` requires touching the frozen `app.py`, which this port
must not edit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .community_common import ensure_community_tables
from .community_public_id import lookup_public_ids_sync


@dataclass(slots=True, frozen=True)
class ChatMessage:
    """One row of `hub_chat_messages`, shaped for the REST history response."""

    id: int
    community_id: str
    channel_name: str | None
    sender_id: int | None
    sender_platform: str | None
    sender_username: str | None
    sender_avatar_url: str | None
    content: str
    message_type: str
    created_at: str | None


@dataclass(slots=True, frozen=True)
class ChatChannel:
    """One distinct chat channel with aggregate activity, for the channel list."""

    name: str
    message_count: int
    last_message_at: str | None


def _iso(value: Any) -> str | None:
    """`datetime` -> ISO-8601 string, passthrough `None` -- pydal returns `datetime` objects."""
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, str):
        return value
    return str(value)


def get_chat_history(
    dal: Any,
    community_id: int,
    *,
    channel_name: str | None,
    limit: int,
    before: str | None,
) -> tuple[list[ChatMessage], bool]:
    """Fetch up to `limit` (max 100) chat messages, newest-first then reversed to oldest-first.

    Mirrors Node's `getChatHistory`: optional `channel_name` filter,
    optional `before` (ISO timestamp) cursor for pagination.
    """
    ensure_community_tables(dal)
    effective_limit = min(max(limit, 1), 100)

    query = dal.hub_chat_messages.community_id == community_id
    if channel_name:
        query &= dal.hub_chat_messages.channel_name == channel_name
    if before:
        query &= dal.hub_chat_messages.created_at < before

    rows = dal(query).select(
        orderby=~dal.hub_chat_messages.created_at, limitby=(0, effective_limit)
    )

    public_id = lookup_public_ids_sync(dal, [community_id])[community_id]
    messages = [
        ChatMessage(
            id=row.id,
            community_id=public_id,
            channel_name=row.channel_name,
            sender_id=row.sender_hub_user_id,
            sender_platform=row.sender_platform,
            sender_username=row.sender_username,
            sender_avatar_url=row.sender_avatar_url,
            content=row.message_content,
            message_type=row.message_type,
            created_at=_iso(row.created_at),
        )
        for row in reversed(rows)
    ]
    has_more = len(rows) == effective_limit
    return messages, has_more


def get_chat_channels(dal: Any, community_id: int) -> list[ChatChannel]:
    """Distinct channels with message count + last-message time, `general` always present."""
    ensure_community_tables(dal)
    rows = dal.executesql(
        """
        SELECT channel_name, COUNT(*) AS message_count, MAX(created_at) AS last_message_at
        FROM hub_chat_messages
        WHERE community_id = $1
        GROUP BY channel_name
        ORDER BY last_message_at DESC
        """,
        placeholders=[community_id],
    )

    channels = [
        ChatChannel(
            name=row[0] or "general",
            message_count=int(row[1]),
            last_message_at=_iso(row[2]),
        )
        for row in rows
    ]
    if not any(c.name == "general" for c in channels):
        channels.insert(0, ChatChannel(name="general", message_count=0, last_message_at=None))
    return channels


@dataclass(slots=True, frozen=True)
class ChatHistoryResponse:
    """Response DTO for `GET .../chat/history`."""

    success: bool
    messages: list[ChatMessage] = field(default_factory=list)
    has_more: bool = False


@dataclass(slots=True, frozen=True)
class ChatChannelsResponse:
    """Response DTO for `GET .../chat/channels`."""

    success: bool
    channels: list[ChatChannel] = field(default_factory=list)
