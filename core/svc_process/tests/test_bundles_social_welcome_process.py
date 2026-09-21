"""Tests for social_welcome_process bundle."""

from __future__ import annotations

import pytest
from flask_core import PlatformEvent, bundle_context, reset_bundle_dal_for_tests, set_bundle_dal
from penguin_dal import AsyncDB
from sqlalchemy import text as sa_text

from bundles.social_welcome_process import (
    _build_welcome,
    _is_first_time,
    _try_mark_welcomed,
    transform,
)


def _event(
    text: str = "hello",
    author_id: str = "user123",
    actor: str = "penguin",
) -> PlatformEvent:
    """Create a test PlatformEvent."""
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor=actor,
        payload={
            "text": text,
            "author_id": author_id,
            "channel_id": "chan-1",
        },
        occurred_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
async def dal():
    """In-memory penguin_dal.AsyncDB with the two real tables this bundle touches."""
    db = AsyncDB("sqlite://", pool_size=1, echo=False)
    async with db.engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE activity_message_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "community_id INTEGER, platform TEXT, platform_user_id TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE community_welcomed_users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "community_id INTEGER, platform TEXT, platform_user_id TEXT, "
                "UNIQUE(community_id, platform, platform_user_id))"
            )
        )
    await db.reflect()
    set_bundle_dal(db)
    yield db
    reset_bundle_dal_for_tests()
    await db.close()


@pytest.fixture(autouse=True)
def reset_bundle_dal() -> None:
    """Reset the bundle DAL before each test."""
    yield
    reset_bundle_dal_for_tests()


class TestIsFirstTime:
    """Tests for _is_first_time."""

    async def test_returns_true_if_no_prior_events(self, dal) -> None:
        """User with no prior activity_message_events is a first-timer."""
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _is_first_time("discord", "user123")
        assert result is True

    async def test_returns_false_if_prior_events_exist(self, dal) -> None:
        """User with prior events is not a first-timer."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO activity_message_events "
                    "(community_id, platform, platform_user_id) VALUES (42, 'discord', 'user123')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _is_first_time("discord", "user123")
        assert result is False

    async def test_scopes_by_community_platform_and_user(self, dal) -> None:
        """A row for a different community/platform/user must not count as a prior event."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO activity_message_events "
                    "(community_id, platform, platform_user_id) "
                    "VALUES (99, 'twitch', 'someone_else')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _is_first_time("discord", "user123")
        assert result is True


class TestTryMarkWelcomed:
    """Tests for _try_mark_welcomed."""

    async def test_returns_true_if_insert_succeeded(self, dal) -> None:
        """Returning a row means this call won the race."""
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _try_mark_welcomed("discord", "user123")
        assert result is True

    async def test_returns_false_if_conflict_prevented_insert(self, dal) -> None:
        """No returned row means a concurrent insert already claimed the welcome."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_welcomed_users "
                    "(community_id, platform, platform_user_id) VALUES (42, 'discord', 'user123')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _try_mark_welcomed("discord", "user123")
        assert result is False

    async def test_scopes_conflict_by_community_platform_and_user(self, dal) -> None:
        """A conflicting row for a DIFFERENT community/platform/user must not block this insert."""
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_welcomed_users "
                    "(community_id, platform, platform_user_id) "
                    "VALUES (99, 'twitch', 'someone_else')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await _try_mark_welcomed("discord", "user123")
        assert result is True


class TestBuildWelcome:
    """Tests for _build_welcome."""

    async def test_returns_template_and_source(self) -> None:
        """Welcome message includes username and is marked as template source."""
        text, source = await _build_welcome(platform_username="alice")
        assert "alice" in text
        assert source == "template"

    async def test_different_username_in_template(self) -> None:
        """Template respects the provided username."""
        text1, _ = await _build_welcome(platform_username="alice")
        text2, _ = await _build_welcome(platform_username="bob")
        assert "alice" in text1
        assert "bob" in text2
        assert text1 != text2


class TestTransform:
    """Tests for transform entrypoint."""

    async def test_missing_text_raises_valueerror(self, dal) -> None:
        """Missing 'text' in payload raises ValueError."""
        event = _event(text="")
        event.payload.pop("text", None)
        with pytest.raises(ValueError, match="text"):
            with bundle_context(
                tenant="acme", community="42", app_id="waddles.social.welcome.default"
            ):
                await transform(event)

    async def test_missing_author_id_raises_valueerror(self, dal) -> None:
        """Missing 'author_id' in payload raises ValueError."""
        event = _event()
        event.payload.pop("author_id", None)
        with pytest.raises(ValueError, match="author_id"):
            with bundle_context(
                tenant="acme", community="42", app_id="waddles.social.welcome.default"
            ):
                await transform(event)

    async def test_missing_actor_raises_valueerror(self, dal) -> None:
        """Missing event.actor raises ValueError."""
        event = _event(actor="")
        with pytest.raises(ValueError, match="event.actor"):
            with bundle_context(
                tenant="acme", community="42", app_id="waddles.social.welcome.default"
            ):
                await transform(event)

    async def test_repeat_visitor_returns_none(self, dal) -> None:
        """Event from a repeat visitor returns None (no welcome)."""
        event = _event()
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO activity_message_events "
                    "(community_id, platform, platform_user_id) VALUES (42, 'discord', 'user123')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await transform(event)
        assert result is None

    async def test_race_condition_returns_none(self, dal) -> None:
        """If another process already marked user as welcomed, return None."""
        event = _event()
        async with dal.engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "INSERT INTO community_welcomed_users "
                    "(community_id, platform, platform_user_id) VALUES (42, 'discord', 'user123')"
                )
            )
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await transform(event)
        assert result is None

    async def test_welcome_claims_and_modifies_event(self, dal) -> None:
        """On successful first-message claim, return event with welcome text."""
        event = _event(text="hello world")
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await transform(event)
        assert isinstance(result, PlatformEvent)
        assert result.payload["text"] != event.payload["text"]
        assert "Welcome" in result.payload["text"]
        assert result.payload["channel_id"] == event.payload["channel_id"]

    async def test_non_string_text_raises_valueerror(self, dal) -> None:
        """Non-string 'text' in payload raises ValueError."""
        event = _event()
        event.payload["text"] = 123
        with pytest.raises(ValueError, match="text"):
            with bundle_context(
                tenant="acme", community="42", app_id="waddles.social.welcome.default"
            ):
                await transform(event)

    async def test_non_string_author_id_raises_valueerror(self, dal) -> None:
        """Non-string 'author_id' raises ValueError."""
        event = _event()
        event.payload["author_id"] = 123
        with pytest.raises(ValueError, match="author_id"):
            with bundle_context(
                tenant="acme", community="42", app_id="waddles.social.welcome.default"
            ):
                await transform(event)

    async def test_empty_author_id_raises_valueerror(self, dal) -> None:
        """Empty 'author_id' raises ValueError."""
        event = _event()
        event.payload["author_id"] = ""
        with pytest.raises(ValueError, match="author_id"):
            with bundle_context(
                tenant="acme", community="42", app_id="waddles.social.welcome.default"
            ):
                await transform(event)

    async def test_welcome_preserves_payload_fields(self, dal) -> None:
        """Modified event preserves original payload fields except text."""
        event = _event(text="hello")
        event.payload["extra_field"] = "should be preserved"
        with bundle_context(tenant="acme", community="42", app_id="waddles.social.welcome.default"):
            result = await transform(event)
        assert result is not None
        assert result.payload["extra_field"] == "should be preserved"
        assert result.platform == event.platform
