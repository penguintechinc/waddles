"""Tests for `builtin_handlers.community_forums_process.transform`."""

from __future__ import annotations

import pytest
from flask_core import PROCESS_TARGET_APP_ID_KEY, PlatformEvent

from builtin_handlers.community_forums_process import _FORUM_APP_ID, _FORUM_USAGE, transform


def _event(text: str) -> PlatformEvent:
    """Create a test event with the given text."""
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor="test_user",
        payload={"text": text, "channel_id": "123"},
        occurred_at="2026-01-01T00:00:00+00:00",
    )


class TestTransformForumCreate:
    """Test forum create command parsing."""

    async def test_parses_valid_create_command(self) -> None:
        """Valid !forum create command should return transformed event."""
        result = await transform(_event("!forum create TestTitle | TestBody"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_action"] == "create"
        assert result.payload["forum_title"] == "TestTitle"
        assert result.payload["forum_body"] == "TestBody"
        assert result.payload["text"] == "TestBody"

    async def test_create_sets_target_app_id_for_cross_app_routing(self) -> None:
        """A successful create must stamp `PROCESS_TARGET_APP_ID_KEY` -> `_FORUM_APP_ID`.

        This is the gh #298 routing fix under test: without this key,
        `core/svc_process/runner.py` enqueues the result onto the
        originating bot's own `:action` key instead of the forums app's,
        and the forum action bundle is never invoked.
        """
        result = await transform(_event("!forum create TestTitle | TestBody"))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _FORUM_APP_ID
        assert _FORUM_APP_ID == "waddles.community.forums.default"

    async def test_create_with_whitespace(self) -> None:
        """Whitespace around delimiters should be stripped."""
        result = await transform(_event("!forum create  Title  |  Body Text  "))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_title"] == "Title"
        assert result.payload["forum_body"] == "Body Text"

    async def test_create_preserves_payload_fields(self) -> None:
        """Forum action should preserve other payload fields."""
        result = await transform(_event("!forum create Title | Body"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["channel_id"] == "123"

    async def test_create_case_insensitive(self) -> None:
        """Command should be case-insensitive."""
        result = await transform(_event("!FORUM CREATE Title | Body"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_action"] == "create"


class TestTransformForumReply:
    """Test forum reply command parsing."""

    async def test_parses_valid_reply_command(self) -> None:
        """Valid !forum reply command should return transformed event."""
        result = await transform(_event("!forum reply 42 | This is a reply"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_action"] == "reply"
        assert result.payload["forum_post_id"] == 42
        assert result.payload["forum_content"] == "This is a reply"

    async def test_reply_sets_target_app_id_for_cross_app_routing(self) -> None:
        """A successful reply must also stamp `PROCESS_TARGET_APP_ID_KEY` -> `_FORUM_APP_ID`."""
        result = await transform(_event("!forum reply 42 | This is a reply"))
        assert isinstance(result, PlatformEvent)
        assert result.payload[PROCESS_TARGET_APP_ID_KEY] == _FORUM_APP_ID

    async def test_reply_with_whitespace(self) -> None:
        """Whitespace around delimiters should be stripped."""
        result = await transform(_event("!forum reply  99  |  Reply text  "))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_post_id"] == 99
        assert result.payload["forum_content"] == "Reply text"

    async def test_reply_case_insensitive(self) -> None:
        """Command should be case-insensitive."""
        result = await transform(_event("!FORUM REPLY 5 | Reply"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_action"] == "reply"


class TestTransformNonForumMessages:
    """Test that non-forum messages return None."""

    async def test_ordinary_chatter_returns_none(self) -> None:
        """Plain chat messages should return None."""
        assert await transform(_event("hello everyone")) is None
        assert await transform(_event("just chatting")) is None

    async def test_other_commands_return_none(self) -> None:
        """Non-forum commands should return None."""
        assert await transform(_event("!ping")) is None
        assert await transform(_event("!help")) is None
        assert await transform(_event("!roll")) is None

    async def test_forum_prefix_only_returns_usage_hint(self) -> None:
        """Incomplete forum command should return a usage-hint reply, not None."""
        result = await transform(_event("!forum"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _FORUM_USAGE
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

        result = await transform(_event("!forum create"))  # no body separator
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _FORUM_USAGE
        assert PROCESS_TARGET_APP_ID_KEY not in result.payload

    async def test_malformed_reply_returns_usage_hint(self) -> None:
        """Reply with non-integer post_id should return a usage-hint reply, not None."""
        result = await transform(_event("!forum reply abc | text"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _FORUM_USAGE

        result = await transform(_event("!forum reply | text"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _FORUM_USAGE

    async def test_malformed_create_returns_usage_hint(self) -> None:
        """Create without body separator should return a usage-hint reply, not None."""
        result = await transform(_event("!forum create Title"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _FORUM_USAGE

    async def test_create_with_empty_body_returns_usage_hint(self) -> None:
        """Create with a title but empty body after `|` should return the usage hint."""
        result = await transform(_event("!forum create Title |   "))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _FORUM_USAGE

    async def test_reply_with_empty_content_returns_usage_hint(self) -> None:
        """Reply with a valid post id but empty content after `|` should return the usage hint."""
        result = await transform(_event("!forum reply 5 |   "))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _FORUM_USAGE


class TestTransformErrorHandling:
    """Test error handling and validation."""

    async def test_missing_text_field_raises(self) -> None:
        """Missing 'text' in payload should raise ValueError."""
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="test_user",
            payload={"channel_id": "123"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)

    async def test_non_string_text_raises(self) -> None:
        """Non-string 'text' in payload should raise ValueError."""
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="test_user",
            payload={"text": 123, "channel_id": "123"},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)

    async def test_empty_text_returns_none(self) -> None:
        """Empty text should return None."""
        assert await transform(_event("")) is None
        assert await transform(_event("   ")) is None


class TestTransformEdgeCases:
    """Test edge cases and boundary conditions."""

    async def test_create_with_multiple_pipes(self) -> None:
        """Only first pipe should be delimiter; rest are content."""
        result = await transform(_event("!forum create Title | Body | with | pipes"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_title"] == "Title"
        assert result.payload["forum_body"] == "Body | with | pipes"

    async def test_reply_with_pipe_in_content(self) -> None:
        """Pipes in reply content should be preserved."""
        result = await transform(_event("!forum reply 1 | Reply with | pipe"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_content"] == "Reply with | pipe"

    async def test_very_long_title(self) -> None:
        """Long titles should be accepted (DB validation elsewhere)."""
        long_title = "x" * 1000
        result = await transform(_event(f"!forum create {long_title} | body"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_title"] == long_title

    async def test_special_characters_in_content(self) -> None:
        """Special characters should be preserved."""
        result = await transform(_event("!forum create Title | Body with @#$%^&*()"))
        assert isinstance(result, PlatformEvent)
        assert "@#$%^&*()" in result.payload["forum_body"]

    async def test_unicode_in_content(self) -> None:
        """Unicode should be handled correctly."""
        result = await transform(_event("!forum create 标题 | 正文 🎉"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["forum_title"] == "标题"
        assert "🎉" in result.payload["forum_body"]


class TestTransformRegression:
    """Regression tests for known issues."""

    async def test_forum_create_relay_format(self) -> None:
        # regression: gh-108
        """Forum post creation should preserve relay-compatible format."""
        result = await transform(_event("!forum create Test Post | Test body content"))
        assert isinstance(result, PlatformEvent)
        # Ensure payload has all fields action stage expects for relay
        assert "forum_action" in result.payload
        assert "forum_title" in result.payload
        assert "forum_body" in result.payload
        assert result.payload["text"] is not None

    async def test_forum_reply_relay_format(self) -> None:
        # regression: gh-108
        """Forum reply creation should preserve relay-compatible format."""
        result = await transform(_event("!forum reply 10 | Reply to post"))
        assert isinstance(result, PlatformEvent)
        # Ensure payload has all fields action stage expects for relay
        assert "forum_action" in result.payload
        assert "forum_post_id" in result.payload
        assert "forum_content" in result.payload
        assert result.payload["text"] is not None
