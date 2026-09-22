"""Tests for `bundles.community_announcements_process.transform`.

`transform`'s announcement lookup goes through `penguin_dal`'s own query
builder (`dal.announcements.<column>` / `dal(query).select()`, D21a), so the
fixture below is a real in-memory SQLite `penguin_dal.AsyncDB` with an
`announcements` table, seeded per test, rather than a hand-rolled fake
mimicking pydal's query-chaining protocol.
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

from bundles.community_announcements_process import _ANNOUNCE_USAGE, transform


async def _add_announcement(
    dal: AsyncDB,
    *,
    id: int,  # matches the row's own column name
    title: str,
    content: str,
    community_id: int,
    announcement_type: str = "general",
    status: str = "published",
    broadcasted_platforms: list[str] | None = None,
) -> None:
    """Insert one `announcements` row."""
    import json as json_module

    async with dal.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "INSERT INTO announcements "
                "(id, community_id, title, content, announcement_type, status, "
                "broadcasted_platforms) "
                "VALUES (:id, :cid, :title, :content, :atype, :status, :platforms)"
            ),
            {
                "id": id,
                "cid": community_id,
                "title": title,
                "content": content,
                "atype": announcement_type,
                "status": status,
                "platforms": json_module.dumps(broadcasted_platforms)
                if broadcasted_platforms is not None
                else None,
            },
        )


@pytest.fixture
async def dal() -> Any:
    """In-memory `penguin_dal.AsyncDB` with an `announcements` table."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE announcements ("
                "id INTEGER PRIMARY KEY, community_id INTEGER NOT NULL, "
                "title TEXT NOT NULL, content TEXT NOT NULL, "
                "announcement_type TEXT DEFAULT 'general', "
                "status TEXT DEFAULT 'published', "
                "broadcasted_platforms JSON)"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


def _event(text: str, **payload_overrides: object) -> PlatformEvent:
    """Build a test PlatformEvent with the given text and payload overrides."""
    payload = {"text": text, "channel_id": "chan-1"}
    payload.update(payload_overrides)
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor="testuser",
        payload=payload,
        occurred_at="2026-09-04T00:00:00Z",
    )


class TestTransform:
    """Tests for announcement command parsing and enrichment."""

    async def test_announce_publish_command_with_valid_id(self, dal: AsyncDB) -> None:
        """Test parsing of `!announce publish <id>` with a valid announcement ID."""
        await _add_announcement(
            dal,
            id=42,
            title="Test Announcement",
            content="This is a test announcement",
            announcement_type="general",
            status="published",
            community_id=42,  # Match the community in the context
            broadcasted_platforms=["discord", "twitch"],
        )

        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish 42"))

        assert result is not None
        assert isinstance(result, PlatformEvent)
        assert result.payload["announcement_id"] == 42
        assert result.payload["announcement"]["title"] == "Test Announcement"
        assert "discord" in result.payload["target_platforms"]
        assert "twitch" in result.payload["target_platforms"]
        # Original fields preserved
        assert result.payload["channel_id"] == "chan-1"
        assert result.platform == "discord"

    async def test_announce_publish_command_case_insensitive(self, dal: AsyncDB) -> None:
        """Test that the command parser is case-insensitive."""
        await _add_announcement(
            dal,
            id=99,
            title="Uppercase Test",
            content="test",
            announcement_type="event",
            status="published",
            community_id=42,
            broadcasted_platforms=[],
        )

        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!ANNOUNCE PUBLISH 99"))

        assert result is not None
        assert result.payload["announcement_id"] == 99

    async def test_ordinary_chatter_returns_none(self, dal: AsyncDB) -> None:
        """Test that non-announcement messages return None (no reply)."""
        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("just chatting"))
        assert result is None

    async def test_partial_command_returns_usage_hint(self, dal: AsyncDB) -> None:
        """Test that an incomplete `!announce` command gets a usage-hint reply, not None."""
        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _ANNOUNCE_USAGE

    async def test_other_bot_commands_return_none(self, dal: AsyncDB) -> None:
        """Test that other bot commands (not announce) return None."""
        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!ping"))
        assert result is None

    async def test_announcement_not_found_returns_none(self, dal: AsyncDB) -> None:
        """Test that non-existent announcements return None."""
        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish 999"))
        assert result is None

    async def test_invalid_announcement_id_returns_usage_hint(self, dal: AsyncDB) -> None:
        """Test that a non-numeric announcement id gets a usage-hint reply, not None."""
        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish abc"))
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] == _ANNOUNCE_USAGE

    async def test_missing_text_field_raises(self, dal: AsyncDB) -> None:
        """Test that missing text field in payload raises ValueError."""
        event = PlatformEvent(
            platform="discord",
            event_type="message",
            actor="testuser",
            payload={"channel_id": "chan-1"},  # Missing 'text'
            occurred_at="2026-09-04T00:00:00Z",
        )
        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            with pytest.raises(ValueError, match="text"):
                await transform(event)

    async def test_enriched_event_preserves_top_level_fields(self, dal: AsyncDB) -> None:
        """Test that enrichment preserves all top-level PlatformEvent fields."""
        await _add_announcement(
            dal,
            id=55,
            title="Title",
            content="content",
            announcement_type="update",
            status="published",
            community_id=42,
            broadcasted_platforms=["discord"],
        )

        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish 55"))

        assert result is not None
        assert result.platform == "discord"
        assert result.event_type == "message"
        assert result.actor == "testuser"
        assert result.occurred_at == "2026-09-04T00:00:00Z"

    async def test_defaults_to_all_platforms_if_not_specified(self, dal: AsyncDB) -> None:
        """Test that bundles default to all platforms if none specified."""
        await _add_announcement(
            dal,
            id=77,
            title="Title",
            content="content",
            announcement_type="general",
            status="published",
            community_id=42,
            broadcasted_platforms=[],  # Empty list
        )

        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish 77"))

        assert result is not None
        # Should default to discord + twitch
        assert "discord" in result.payload["target_platforms"]
        assert "twitch" in result.payload["target_platforms"]

    async def test_handles_missing_broadcasted_platforms_attr(self, dal: AsyncDB) -> None:
        """Test handling when broadcasted_platforms is NULL in the row."""
        await _add_announcement(
            dal,
            id=88,
            title="Title",
            content="content",
            announcement_type="general",
            status="published",
            community_id=42,
            broadcasted_platforms=None,
        )

        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish 88"))

        assert result is not None
        # Should default to discord + twitch
        assert "discord" in result.payload["target_platforms"]
        assert "twitch" in result.payload["target_platforms"]

    async def test_handles_invalid_broadcasted_platforms_type(self, dal: AsyncDB) -> None:
        """Test handling when broadcasted_platforms is not a list."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO announcements "
                    "(id, community_id, title, content, announcement_type, status, "
                    "broadcasted_platforms) "
                    "VALUES (89, 42, 'Title', 'content', 'general', 'published', :platforms)"
                ),
                {"platforms": '"discord"'},  # a JSON string, not a list
            )

        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish 89"))

        assert result is not None
        # Should default to discord + twitch when invalid type
        assert "discord" in result.payload["target_platforms"]
        assert "twitch" in result.payload["target_platforms"]

    async def test_db_error_during_query_raises(
        self, dal: AsyncDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that DB query errors are caught and re-raised as ValueError."""

        async def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("Connection failed")

        # `dal(query)` returns an `AsyncQuerySet`; patch its `.select()`.
        monkeypatch.setattr(
            "penguin_dal.query.AsyncQuerySet.select",
            _raise,
        )

        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            with pytest.raises(ValueError, match="failed to lookup or enrich"):
                await transform(_event("!announce publish 100"))

    async def test_cross_community_announcement_not_found(self, dal: AsyncDB) -> None:
        """Regression: announcement from another community should not be found (IDOR prevention)."""
        # Add an announcement belonging to community 99
        await _add_announcement(
            dal,
            id=123,
            title="Other Community Announcement",
            content="This belongs to community 99",
            announcement_type="general",
            status="published",
            community_id=99,  # Different community
            broadcasted_platforms=["discord"],
        )

        # Try to access it from community 42
        with bundle_context(
            tenant="acme-corp", community="42", app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish 123"))

        # Should return None (as if announcement not found) to prevent IDOR
        assert result is None

    async def test_tenant_wide_activation_cannot_broadcast(self, dal: AsyncDB) -> None:
        """Tenant-wide activations (community=None) cannot broadcast announcements."""
        # Even if an announcement exists, tenant-wide activations should return None
        with bundle_context(
            tenant="acme-corp", community=None, app_id="waddles.community.announcements.default"
        ):
            result = await transform(_event("!announce publish 1"))

        assert result is None


class TestRealSqlite:
    """Real-DB smoke (gh-298): exercises the actual `penguin_dal` query path, not a fake."""

    async def test_announce_publish_against_real_sqlite(self, dal: AsyncDB) -> None:
        # regression: gh-298 real-pydal (now real-penguin_dal)
        """Insert one announcement, enrich via `transform`, read back."""
        await _add_announcement(
            dal,
            id=1,
            title="Real Announcement",
            content="Real content",
            community_id=7,
            announcement_type="general",
            status="published",
            broadcasted_platforms=["discord"],
        )

        with bundle_context(
            tenant="acme-corp",
            community="7",
            app_id="waddles.community.announcements.default",
        ):
            result = await transform(_event("!announce publish 1"))

        assert result is not None
        assert result.payload["announcement_id"] == 1
        assert result.payload["announcement"]["title"] == "Real Announcement"
        assert result.payload["target_platforms"] == ["discord"]
