"""Tests for `services/platform_moderation.py::resolve_community_moderator_token` (gh-320).

Only the community-aware resolver helper is tested directly here --
`twitch_timeout`/`discord_timeout`/`discord_warn`/`twitch_warn`'s own real
HTTP/relay call logic is already covered end-to-end via
`test_builtin_moderation_enforce_action.py`, which is also where this
helper's integration into `_enforce_twitch`'s moderator-token resolution
order (community-first, env-fallback) is exercised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest

from services import platform_moderation as mod


@dataclass(slots=True, frozen=True)
class _FakeCommunityTokens:
    """Local stand-in for `waddle_transports.community_credentials.CommunityTokens`.

    Not imported from `waddle_transports` -- that module is landing
    concurrently (gh-320) and may not exist on disk yet; mirrors the
    contract's documented field shape closely enough for
    `resolve_community_moderator_token`'s own `tokens.source`/
    `tokens.access_token` attribute reads.
    """

    access_token: str | None
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]
    source: str


class TestResolveCommunityModeratorToken:
    """`resolve_community_moderator_token()` -- community source / env source / none / failure."""

    async def test_community_source_returns_its_access_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(community_id: int | None, provider: str) -> _FakeCommunityTokens:
            assert community_id == 42
            assert provider == "twitch"
            return _FakeCommunityTokens(
                access_token="community-moderator-token",
                refresh_token=None,
                expires_at=None,
                scopes=["moderator:manage:banned_users"],
                source="community",
            )

        monkeypatch.setattr(mod, "resolve_community_tokens", fake_resolve)

        token = await mod.resolve_community_moderator_token(42)

        assert token == "community-moderator-token"

    async def test_env_source_returns_none_so_caller_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(community_id: int | None, provider: str) -> _FakeCommunityTokens:
            return _FakeCommunityTokens(
                access_token="ignored-env-side-token",
                refresh_token="rtoken",
                expires_at=None,
                scopes=[],
                source="env",
            )

        monkeypatch.setattr(mod, "resolve_community_tokens", fake_resolve)

        token = await mod.resolve_community_moderator_token(42)

        assert token is None

    async def test_no_community_connection_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(community_id: int | None, provider: str) -> None:
            return None

        monkeypatch.setattr(mod, "resolve_community_tokens", fake_resolve)

        token = await mod.resolve_community_moderator_token(42)

        assert token is None

    async def test_resolver_unavailable_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "resolve_community_tokens", None)

        token = await mod.resolve_community_moderator_token(42)

        assert token is None

    async def test_resolver_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_resolve(community_id: int | None, provider: str) -> Any:
            raise RuntimeError("hub-api unreachable")

        monkeypatch.setattr(mod, "resolve_community_tokens", fake_resolve)

        token = await mod.resolve_community_moderator_token(42)

        assert token is None

    async def test_none_community_id_is_passed_through_to_the_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_resolve(community_id: int | None, provider: str) -> None:
            assert community_id is None
            return None

        monkeypatch.setattr(mod, "resolve_community_tokens", fake_resolve)

        token = await mod.resolve_community_moderator_token(None)

        assert token is None
