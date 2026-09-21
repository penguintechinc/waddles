"""Tests for `bundles.community_chat_process.transform`.

`transform`'s two DB-backed commands go through `raw_sql_rows()` (D21a)
against the bound `penguin_dal.AsyncDB` (`get_bundle_dal()`), so the fixture
below is a real in-memory SQLite `AsyncDB` with the two tables/columns the
bundle's raw SQL touches (`hub_chat_messages`, `communities`, `tenants`)
seeded per test, rather than a bare `.execute()` mock.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core import (
    PlatformEvent,
    bundle_context,
    reset_bundle_dal_for_tests,
    set_bundle_dal,
)
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text

from bundles.community_chat_process import (
    ChatChannel,
    ChatMessage,
    _format_channels,
    _format_chat_history,
    transform,
)

TENANT_ID = "tenant-1"
COMMUNITY_ID = 1


async def _add_message(
    dal: AsyncDB,
    *,
    id: int,  # matches the row's own column name
    community_id: int = COMMUNITY_ID,
    channel_name: str | None = "general",
    sender_username: str | None,
    message_content: str,
    message_type: str = "text",
    created_at: str | None,
) -> None:
    """Insert one `hub_chat_messages` row, seeding `communities`/`tenants` if missing."""
    async with dal.engine.begin() as conn:
        await conn.execute(
            sa_text("INSERT OR IGNORE INTO communities (id, tenant_id) VALUES (:cid, :tid)"),
            {"cid": community_id, "tid": TENANT_ID},
        )
        await conn.execute(
            sa_text("INSERT OR IGNORE INTO tenants (id) VALUES (:tid)"), {"tid": TENANT_ID}
        )
        await conn.execute(
            sa_text(
                "INSERT INTO hub_chat_messages "
                "(id, community_id, channel_name, sender_username, message_content, "
                "message_type, created_at) "
                "VALUES (:id, :cid, :channel, :sender, :content, :mtype, :created_at)"
            ),
            {
                "id": id,
                "cid": community_id,
                "channel": channel_name,
                "sender": sender_username,
                "content": message_content,
                "mtype": message_type,
                "created_at": created_at,
            },
        )


@pytest.fixture
async def dal() -> Any:
    """In-memory `penguin_dal.AsyncDB` with `hub_chat_messages`/`communities`/`tenants`."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text("CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id TEXT)")
        )
        await conn.execute(sa_text("CREATE TABLE tenants (id TEXT PRIMARY KEY)"))
        await conn.execute(
            sa_text(
                "CREATE TABLE hub_chat_messages ("
                "id INTEGER PRIMARY KEY, community_id INTEGER, channel_name TEXT, "
                "sender_username TEXT, message_content TEXT, message_type TEXT, "
                "created_at TEXT)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


def _event(text: str, **payload_overrides: object) -> PlatformEvent:
    """Build a test PlatformEvent with optional payload overrides."""
    default_payload = {"text": text, "channel_id": "chan-1"}
    default_payload.update(payload_overrides)
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor="test_user",
        payload=default_payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


class TestTransform:
    """Tests for the transform entrypoint."""

    async def test_chat_history_command_returns_reply(self, dal: AsyncDB) -> None:
        """!chat-history command triggers a reply."""
        await _add_message(
            dal,
            id=1,
            sender_username="alice",
            message_content="hello",
            created_at="2026-01-01T12:00:00Z",
        )
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            result = await transform(_event("!chat-history"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"]
        assert "Chat History" in result.payload["text"]
        assert "alice" in result.payload["text"]

    async def test_channels_command_returns_reply(self, dal: AsyncDB) -> None:
        """!channels command triggers a reply."""
        await _add_message(
            dal,
            id=1,
            sender_username="alice",
            message_content="hello",
            created_at="2026-01-01T12:00:00Z",
        )
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            result = await transform(_event("!channels"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"]
        assert "Chat Channels" in result.payload["text"]
        assert "general" in result.payload["text"]

    async def test_case_insensitive_commands(self, dal: AsyncDB) -> None:
        """Commands are case-insensitive."""
        await _add_message(
            dal,
            id=1,
            sender_username="alice",
            message_content="hello",
            created_at="2026-01-01T12:00:00Z",
        )
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            for cmd in ["!CHAT-HISTORY", "!Chat-History", "!CHANNELS", "!Channels"]:
                result = await transform(_event(cmd))
                assert result is not None, f"Command '{cmd}' should return a reply"
                assert isinstance(result, PlatformEvent)

    async def test_ordinary_chatter_returns_none(self, dal: AsyncDB) -> None:
        """Non-command messages return None (no reply)."""
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            for text in ["hello", "just chatting", "what's up?", "tell me a story"]:
                result = await transform(_event(text))
                assert result is None, f"Text '{text}' should return None"

    async def test_preserves_channel_id_on_reply(self, dal: AsyncDB) -> None:
        """Response preserves the original channel_id in payload."""
        await _add_message(
            dal,
            id=1,
            sender_username="alice",
            message_content="hello",
            created_at="2026-01-01T12:00:00Z",
        )
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            result = await transform(_event("!chat-history"))
        assert result is not None
        assert result.payload["channel_id"] == "chan-1"

    async def test_preserves_other_payload_fields(self, dal: AsyncDB) -> None:
        """Response preserves non-text payload fields."""
        await _add_message(
            dal,
            id=1,
            sender_username="alice",
            message_content="hello",
            created_at="2026-01-01T12:00:00Z",
        )
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            result = await transform(_event("!channels", author_id="123"))
        assert result is not None
        assert result.payload.get("author_id") == "123"
        assert result.payload["channel_id"] == "chan-1"

    async def test_missing_text_returns_none(self, dal: AsyncDB) -> None:
        """Event without 'text' in payload returns None."""
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor=None,
            payload={"channel_id": "1"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            result = await transform(event)
        assert result is None

    async def test_empty_text_returns_none(self, dal: AsyncDB) -> None:
        """Empty or whitespace-only text returns None."""
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            for text in ["", "   ", "\t", "\n"]:
                result = await transform(_event(text))
                assert result is None, f"Text '{text!r}' should return None"

    async def test_non_string_text_returns_none(self, dal: AsyncDB) -> None:
        """Non-string 'text' in payload returns None."""
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            event = PlatformEvent(
                platform="discord",
                event_type="message",
                actor="test_user",
                payload={"text": 123, "channel_id": "chan-1"},
                occurred_at="2026-01-01T00:00:00+00:00",
            )
            result = await transform(event)
            assert result is None

            event2 = PlatformEvent(
                platform="discord",
                event_type="message",
                actor="test_user",
                payload={"text": ["list"], "channel_id": "chan-1"},
                occurred_at="2026-01-01T00:00:00+00:00",
            )
            result2 = await transform(event2)
            assert result2 is None

    async def test_text_with_whitespace_stripped(self, dal: AsyncDB) -> None:
        """Whitespace is stripped before command detection."""
        await _add_message(
            dal,
            id=1,
            sender_username="alice",
            message_content="hello",
            created_at="2026-01-01T12:00:00Z",
        )
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            result = await transform(_event("   !chat-history   "))
        assert result is not None
        assert isinstance(result, PlatformEvent)

    async def test_command_in_middle_of_text_no_reply(self, dal: AsyncDB) -> None:
        """Command word in the middle of text does not trigger reply."""
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            result = await transform(_event("please !chat-history for me"))
        assert result is None

    async def test_reply_preserves_platform_metadata(self, dal: AsyncDB) -> None:
        """Response preserves platform, event_type, actor, occurred_at."""
        await _add_message(
            dal,
            id=1,
            sender_username="alice",
            message_content="hello",
            created_at="2026-01-01T12:00:00Z",
        )
        with bundle_context(
            tenant=TENANT_ID, community=str(COMMUNITY_ID), app_id="waddles.community.chat.default"
        ):
            result = await transform(_event("!channels"))
        assert result is not None
        assert result.platform == "discord"
        assert result.event_type == "message"
        assert result.actor == "test_user"
        assert result.occurred_at == "2026-01-01T00:00:00+00:00"


class TestFormatChatHistory:
    """Tests for _format_chat_history helper."""

    def test_empty_list(self) -> None:
        """Empty message list returns placeholder."""
        result = _format_chat_history([])
        assert result == "(no messages found)"

    def test_single_message(self) -> None:
        """Single message is formatted correctly."""
        msg = ChatMessage(
            id=1,
            community_id=1,
            channel_name="general",
            sender_username="alice",
            content="hello world",
            message_type="text",
            created_at="2026-01-01T12:00:00",
        )
        result = _format_chat_history([msg])
        assert "Chat History" in result
        assert "alice" in result
        assert "hello world" in result
        assert "2026-01-01" in result

    def test_multiple_messages(self) -> None:
        """Multiple messages are all included (up to limit)."""
        msgs = [
            ChatMessage(
                id=i,
                community_id=1,
                channel_name="general",
                sender_username=f"user{i}",
                content=f"message {i}",
                message_type="text",
                created_at="2026-01-01T12:00:00",
            )
            for i in range(5)
        ]
        result = _format_chat_history(msgs)
        for i in range(5):
            assert f"user{i}" in result

    def test_truncates_long_messages(self) -> None:
        """Long message content is truncated in output."""
        msg = ChatMessage(
            id=1,
            community_id=1,
            channel_name="general",
            sender_username="alice",
            content="x" * 200,
            message_type="text",
            created_at="2026-01-01T12:00:00",
        )
        result = _format_chat_history([msg])
        assert len(result) < 4100  # must fit in platform limit

    def test_truncates_entire_output_if_too_long(self) -> None:
        """Entire output is capped at ~4000 chars."""
        msgs = [
            ChatMessage(
                id=i,
                community_id=1,
                channel_name="general",
                sender_username=f"very_long_username_{i}",
                content="message content " * 10,
                message_type="text",
                created_at="2026-01-01T12:00:00",
            )
            for i in range(50)
        ]
        result = _format_chat_history(msgs)
        assert len(result) <= 4100

    def test_missing_created_at(self) -> None:
        """Message with None created_at renders gracefully."""
        msg = ChatMessage(
            id=1,
            community_id=1,
            channel_name="general",
            sender_username="alice",
            content="hello",
            message_type="text",
            created_at=None,
        )
        result = _format_chat_history([msg])
        assert "?" in result  # placeholder for missing timestamp

    def test_missing_sender_username(self) -> None:
        """Message with None sender_username renders as 'unknown'."""
        msg = ChatMessage(
            id=1,
            community_id=1,
            channel_name="general",
            sender_username=None,
            content="hello",
            message_type="text",
            created_at="2026-01-01T12:00:00",
        )
        result = _format_chat_history([msg])
        assert "unknown" in result


class TestFormatChannels:
    """Tests for _format_channels helper."""

    def test_empty_list(self) -> None:
        """Empty channel list returns placeholder."""
        result = _format_channels([])
        assert result == "(no channels found)"

    def test_single_channel(self) -> None:
        """Single channel is formatted correctly."""
        ch = ChatChannel(name="general", message_count=42, last_message_at="2026-01-01T12:00:00")
        result = _format_channels([ch])
        assert "Chat Channels" in result
        assert "general" in result
        assert "42" in result

    def test_multiple_channels(self) -> None:
        """Multiple channels are all listed."""
        channels = [
            ChatChannel(name="general", message_count=100, last_message_at="2026-01-01T12:00:00"),
            ChatChannel(name="random", message_count=50, last_message_at="2026-01-01T11:00:00"),
            ChatChannel(
                name="announcements", message_count=10, last_message_at="2026-01-01T10:00:00"
            ),
        ]
        result = _format_channels(channels)
        for ch in channels:
            assert ch.name in result

    def test_truncates_if_too_long(self) -> None:
        """Output is capped at ~4000 chars if needed."""
        channels = [
            ChatChannel(
                name=f"channel_with_a_very_long_name_{i}",
                message_count=1000000,
                last_message_at="2026-01-01T12:00:00",
            )
            for i in range(100)
        ]
        result = _format_channels(channels)
        assert len(result) <= 4100
