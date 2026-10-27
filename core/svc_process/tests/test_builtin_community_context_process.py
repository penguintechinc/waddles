"""Tests for `builtin_handlers.community_context_process` -- `!cc` community-context switch."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from flask_core import PlatformEvent, bundle_context

from builtin_handlers.community_context_process import (
    _GUARD_REPLY,
    _NO_LINKED_COMMUNITIES_REPLY,
    ChannelCommunity,
    transform,
)

TENANT = "acme"
COMMUNITY = "4"
APP_ID = "waddles.bot.discord.default"


def _event(text: str, **payload_overrides: object) -> PlatformEvent:
    """Create a test event with the given text; a channel_id + tokenized author_id by default."""
    payload: dict[str, object] = {
        "text": text,
        "channel_id": "chan-1",
        "author_id": "platform-user-1",
        **payload_overrides,
    }
    return PlatformEvent(
        platform="discord",
        event_type="message",
        actor="test_user",
        payload=payload,
        occurred_at="2026-01-01T00:00:00+00:00",
    )


async def _flag_on(*_args: Any, **_kwargs: Any) -> bool:
    return True


async def _flag_off(*_args: Any, **_kwargs: Any) -> bool:
    return False


@pytest.fixture(autouse=True)
def _flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to flag ON -- the OFF-specific test overrides this explicitly."""
    monkeypatch.setattr("builtin_handlers.community_context_process.feature_enabled", _flag_on)


def _communities(*rows: tuple[int, str, bool]) -> list[ChannelCommunity]:
    """Build `ChannelCommunity` rows from `(id, name, is_primary)` tuples."""
    return [ChannelCommunity(id=i, name=n, is_primary=p) for i, n, p in rows]


class _FakeStore:
    """In-memory stand-in for `services.community_context_store`'s four functions."""

    def __init__(
        self,
        *,
        communities: list[ChannelCommunity] | None = None,
        override: int | None = None,
        raise_on_list: bool = False,
        raise_on_get: bool = False,
        raise_on_set: bool = False,
        raise_on_clear: bool = False,
    ) -> None:
        self.communities = communities if communities is not None else []
        self.override = override
        self.raise_on_list = raise_on_list
        self.raise_on_get = raise_on_get
        self.raise_on_set = raise_on_set
        self.raise_on_clear = raise_on_clear
        self.list_calls = 0
        self.get_calls = 0
        self.set_calls: list[dict[str, object]] = []
        self.clear_calls: list[dict[str, object]] = []

    async def list_channel_communities(
        self, *, platform: str, platform_entity_id: str
    ) -> list[ChannelCommunity]:
        self.list_calls += 1
        if self.raise_on_list:
            raise RuntimeError("simulated store outage")
        return self.communities

    async def get_context(
        self, *, platform: str, platform_user_id: str, platform_entity_id: str
    ) -> int | None:
        self.get_calls += 1
        if self.raise_on_get:
            raise RuntimeError("simulated store outage")
        return self.override

    async def set_context(
        self,
        *,
        platform: str,
        platform_user_id: str,
        platform_entity_id: str,
        community_id: int,
        ttl_s: int = 86400,
    ) -> None:
        if self.raise_on_set:
            raise RuntimeError("simulated store outage")
        self.set_calls.append(
            {
                "platform": platform,
                "platform_user_id": platform_user_id,
                "platform_entity_id": platform_entity_id,
                "community_id": community_id,
                "ttl_s": ttl_s,
            }
        )
        self.override = community_id

    async def clear_context(
        self, *, platform: str, platform_user_id: str, platform_entity_id: str
    ) -> None:
        if self.raise_on_clear:
            raise RuntimeError("simulated store outage")
        self.clear_calls.append(
            {
                "platform": platform,
                "platform_user_id": platform_user_id,
                "platform_entity_id": platform_entity_id,
            }
        )
        self.override = None


def _wire(monkeypatch: pytest.MonkeyPatch, store: _FakeStore) -> None:
    """Monkeypatch the four store functions bound into `community_context_process`'s namespace."""
    monkeypatch.setattr(
        "builtin_handlers.community_context_process.list_channel_communities",
        store.list_channel_communities,
    )
    monkeypatch.setattr("builtin_handlers.community_context_process.get_context", store.get_context)
    monkeypatch.setattr("builtin_handlers.community_context_process.set_context", store.set_context)
    monkeypatch.setattr(
        "builtin_handlers.community_context_process.clear_context", store.clear_context
    )


async def _run(text: str, **event_kwargs: object) -> PlatformEvent | None:
    with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
        return await transform(_event(text, **event_kwargs))


class TestRouting:
    async def test_non_command_text_returns_none(self) -> None:
        assert await transform(_event("just chatting")) is None

    async def test_malformed_event_missing_text_raises_value_error(self) -> None:
        event = PlatformEvent(
            platform="discord", event_type="message", actor="p", payload={}, occurred_at="x"
        )
        with pytest.raises(ValueError, match="text"):
            await transform(event)

    async def test_bare_bang_returns_none(self) -> None:
        assert await transform(_event("!")) is None

    async def test_other_command_returns_none(self) -> None:
        assert await transform(_event("!poll")) is None

    async def test_cc_prefix_of_longer_word_does_not_match(self) -> None:
        assert await transform(_event("!ccfoo")) is None


class TestFlag:
    async def test_flag_off_returns_none_and_never_touches_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("builtin_handlers.community_context_process.feature_enabled", _flag_off)
        store = _FakeStore(raise_on_list=True)  # would blow up if ever called
        _wire(monkeypatch, store)
        assert await _run("!cc") is None
        assert store.list_calls == 0


class TestIdentityValidation:
    async def test_missing_author_id_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, _FakeStore())
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="author_id"):
                await transform(_event("!cc", author_id=None))

    async def test_missing_channel_id_and_name_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, _FakeStore())
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            with pytest.raises(ValueError, match="channel_id"):
                await transform(_event("!cc", channel_id=None))

    async def test_channel_name_fallback_used_when_channel_id_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Twitch events carry `channel_name`, not `channel_id` -- see module docstring."""
        store = _FakeStore(communities=_communities((1, "HQ", True)))
        _wire(monkeypatch, store)
        with bundle_context(tenant=TENANT, community=COMMUNITY, app_id=APP_ID):
            result = await transform(_event("!cc", channel_id=None, channel_name="pengu_channel"))
        assert result is not None
        assert "HQ" in result.payload["text"]


class TestZeroLinkedCommunities:
    async def test_bare_cc_replies_not_linked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, _FakeStore(communities=[]))
        result = await _run("!cc")
        assert result is not None
        assert result.payload["text"] == _NO_LINKED_COMMUNITIES_REPLY

    async def test_switch_also_replies_not_linked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _wire(monkeypatch, _FakeStore(communities=[]))
        result = await _run("!cc somewhere")
        assert result is not None
        assert result.payload["text"] == _NO_LINKED_COMMUNITIES_REPLY


class TestSingleLinkedCommunity:
    async def test_bare_cc_replies_only_community(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore(communities=_communities((1, "HQ", True)))
        _wire(monkeypatch, store)
        result = await _run("!cc")
        assert result is not None
        assert result.payload["text"] == "community context: HQ (only community on this channel)"
        assert store.get_calls == 0  # never needs the per-user override lookup


class TestStatusMultipleCommunities:
    async def test_default_current_when_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore(
            communities=_communities((1, "Alpha", True), (2, "Beta", False), (3, "Gamma", False)),
            override=None,
        )
        _wire(monkeypatch, store)
        result = await _run("!cc")
        assert result is not None
        assert result.payload["text"] == (
            "community context: Alpha (default) — available: Beta, Gamma"
        )

    async def test_shows_active_override_without_default_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore(
            communities=_communities((1, "Alpha", True), (2, "Beta", False), (3, "Gamma", False)),
            override=2,
        )
        _wire(monkeypatch, store)
        result = await _run("!cc")
        assert result is not None
        assert result.payload["text"] == "community context: Beta — available: Alpha, Gamma"

    async def test_get_context_failure_is_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore(
            communities=_communities((1, "Alpha", True), (2, "Beta", False)),
            raise_on_get=True,
        )
        _wire(monkeypatch, store)
        result = await _run("!cc")
        assert result is not None
        assert result.payload["text"] == _GUARD_REPLY

    async def test_list_communities_failure_is_guarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wire(monkeypatch, _FakeStore(raise_on_list=True))
        result = await _run("!cc")
        assert result is not None
        assert result.payload["text"] == _GUARD_REPLY


class TestSwitch:
    async def test_switch_matches_case_insensitively(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore(communities=_communities((1, "Alpha", True), (2, "Beta", False)))
        _wire(monkeypatch, store)
        result = await _run("!cc beta")
        assert result is not None
        assert result.payload["text"] == "switched to Beta for 24h"
        assert store.set_calls == [
            {
                "platform": "discord",
                "platform_user_id": "platform-user-1",
                "platform_entity_id": "chan-1",
                "community_id": 2,
                "ttl_s": 86400,
            }
        ]

    async def test_switch_treats_hyphens_and_spaces_as_equal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore(
            communities=_communities((1, "Alpha", True), (2, "Board Game Night", False))
        )
        _wire(monkeypatch, store)
        result = await _run("!cc board-game-night")
        assert result is not None
        assert result.payload["text"] == "switched to Board Game Night for 24h"

    async def test_switch_no_match_lists_available_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore(communities=_communities((1, "Alpha", True), (2, "Beta", False)))
        _wire(monkeypatch, store)
        result = await _run("!cc nope")
        assert result is not None
        assert result.payload["text"] == (
            "no community named 'nope' on this channel — try: Alpha, Beta"
        )
        assert store.set_calls == []

    async def test_set_context_failure_is_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore(
            communities=_communities((1, "Alpha", True), (2, "Beta", False)), raise_on_set=True
        )
        _wire(monkeypatch, store)
        result = await _run("!cc beta")
        assert result is not None
        assert result.payload["text"] == _GUARD_REPLY


class TestReset:
    @pytest.mark.parametrize("subcommand", ["default", "reset", "DEFAULT", "Reset"])
    async def test_default_and_reset_clear_override(
        self, monkeypatch: pytest.MonkeyPatch, subcommand: str
    ) -> None:
        store = _FakeStore(
            communities=_communities((1, "Alpha", True), (2, "Beta", False)), override=2
        )
        _wire(monkeypatch, store)
        result = await _run(f"!cc {subcommand}")
        assert result is not None
        assert result.payload["text"] == "community context reset to Alpha (default)"
        assert len(store.clear_calls) == 1

    async def test_clear_context_failure_is_guarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _FakeStore(
            communities=_communities((1, "Alpha", True), (2, "Beta", False)), raise_on_clear=True
        )
        _wire(monkeypatch, store)
        result = await _run("!cc reset")
        assert result is not None
        assert result.payload["text"] == _GUARD_REPLY


class TestFeatureRegistration:
    """`bot_process._FEATURE_MODULES["cc"]` must route to this bundle -- see gh #311."""

    def test_cc_registered_in_bot_process_feature_modules(self) -> None:
        from builtin_handlers.bot_process import _FEATURE_MODULES

        assert _FEATURE_MODULES["cc"] == "builtin_handlers.community_context_process"

    def test_registered_module_path_imports_and_exposes_transform(self) -> None:
        from builtin_handlers.bot_process import _FEATURE_MODULES

        module = importlib.import_module(_FEATURE_MODULES["cc"])
        assert callable(module.transform)
