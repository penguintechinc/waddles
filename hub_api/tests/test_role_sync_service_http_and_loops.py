"""Coverage-gap tests for `role_sync_service`: real httpx clients, loop-prevention, batch/main.

The Twitch/Discord clients run against real `httpx` request/response objects via
`httpx.MockTransport` (no socket) so classification, pagination and error-wrapping
branches execute for real. Loop-prevention is asserted structurally: the
discord->platform direction must never call Discord's add/remove-role API and the
twitch->discord direction must never write `community_members.role`.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from services import role_sync_service as svc
from services.credential_resolver import TransportUnavailable
from tests.test_role_sync_service import (
    _DISCORD_PLATFORM_USER,
    _DISCORD_SUB_USER,
    _MEMBER_ROLE_DISCORD,
    _TWITCH_SUB_USER,
    _add_community_member,
    _always_token,
    _FailingCredentialResolver,
    _FakeCredentialResolver,
    _FakeDiscordClient,
    _FakeTwitchClient,
    _get_member_role,
    _link_identity,
    _make_pairing,
    _make_platform_pairing,
)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the PostHog flag ON; individual tests flip it OFF."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "feature_enabled", stub)
    return stub


def _resp(status: int, body: Any = None) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {})


def _twitch(handler: Callable[[httpx.Request], httpx.Response]) -> svc.HttpTwitchRoleSourceClient:
    return svc.HttpTwitchRoleSourceClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), api_base="https://t.test"
    )


def _discord(handler: Callable[[httpx.Request], httpx.Response]) -> svc.HttpDiscordRoleTargetClient:
    return svc.HttpDiscordRoleTargetClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        bot_token="bot-tok",  # noqa: S106
        api_base="https://d.test",
    )


def _boom(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down", request=request)


class TestClassifiers:
    @pytest.mark.parametrize(
        ("status", "needle"),
        [(401, "401"), (403, "403"), (429, "429"), (500, "HTTP 500")],
    )
    def test_twitch_error_statuses_raise_specific(self, status: int, needle: str) -> None:
        with pytest.raises(svc.TwitchSyncError, match=needle):
            svc._classify_twitch(_resp(status), action="x")

    def test_twitch_2xx_ok(self) -> None:
        assert svc._classify_twitch(_resp(204), action="x") is None

    @pytest.mark.parametrize(
        ("status", "needle"),
        [(401, "401"), (403, "403"), (429, "429"), (502, "HTTP 502")],
    )
    def test_discord_error_statuses_raise_specific(self, status: int, needle: str) -> None:
        with pytest.raises(svc.DiscordSyncError, match=needle):
            svc._classify_discord(_resp(status), action="x")

    def test_discord_404_is_not_an_error(self) -> None:
        assert svc._classify_discord(_resp(404), action="x") is None


class TestHttpTwitchClient:
    async def test_broadcaster_id_ok_sends_user_token_and_client_id(self) -> None:
        seen: dict[str, str] = {}

        def h(req: httpx.Request) -> httpx.Response:
            seen.update(req.headers)
            assert req.url.path == "/users"
            return _resp(200, {"data": [{"id": 4242}]})

        got = await _twitch(h).get_broadcaster_id(user_token="utok", client_id="cid")  # noqa: S106
        assert got == "4242"
        assert seen["authorization"] == "Bearer utok"
        assert seen["client-id"] == "cid"

    async def test_broadcaster_id_empty_data_fails_loud(self) -> None:
        with pytest.raises(svc.TwitchSyncError, match="no user"):
            await _twitch(lambda r: _resp(200, {"data": []})).get_broadcaster_id(
                user_token="t",
                client_id="c",  # noqa: S106
            )

    async def test_broadcaster_id_401_and_network_error(self) -> None:
        with pytest.raises(svc.TwitchSyncError, match="401"):
            await _twitch(lambda r: _resp(401)).get_broadcaster_id(
                user_token="t",
                client_id="c",  # noqa: S106
            )
        with pytest.raises(svc.TwitchSyncError, match="self-lookup request failed"):
            await _twitch(_boom).get_broadcaster_id(user_token="t", client_id="c")  # noqa: S106

    async def test_subscriber_tiers_paginates_and_skips_malformed_rows(self) -> None:
        calls: list[str | None] = []

        def h(req: httpx.Request) -> httpx.Response:
            after = req.url.params.get("after")
            calls.append(after)
            if after is None:
                return _resp(
                    200,
                    {
                        "data": [
                            {"user_id": "1", "tier": "1000"},
                            {"user_id": "2", "tier": "3000"},
                            {"user_id": "", "tier": "1000"},  # no user
                            {"user_id": "3", "tier": ""},  # no tier
                            {"user_id": "4", "tier": "abc"},  # not int
                            {"user_id": "5", "tier": "9000"},  # out of range
                        ],
                        "pagination": {"cursor": "next"},
                    },
                )
            return _resp(200, {"data": [{"user_id": "6", "tier": "2000"}], "pagination": {}})

        tiers = await _twitch(h).list_subscriber_tiers(
            broadcaster_id="b",
            user_token="t",
            client_id="c",  # noqa: S106
        )
        assert tiers == {"1": 1, "2": 3, "6": 2}
        assert calls == [None, "next"]

    async def test_moderators_collects_ids_and_ignores_missing(self) -> None:
        mods = await _twitch(
            lambda r: _resp(200, {"data": [{"user_id": "7"}, {"user_id": None}, {}]})
        ).list_moderators(broadcaster_id="b", user_token="t", client_id="c")  # noqa: S106
        assert mods == {"7"}

    async def test_paginate_network_and_status_errors(self) -> None:
        with pytest.raises(svc.TwitchSyncError, match="request failed"):
            await _twitch(_boom).list_moderators(
                broadcaster_id="b",
                user_token="t",
                client_id="c",  # noqa: S106
            )
        with pytest.raises(svc.TwitchSyncError, match="403"):
            await _twitch(lambda r: _resp(403)).list_subscriber_tiers(
                broadcaster_id="b",
                user_token="t",
                client_id="c",  # noqa: S106
            )


class TestHttpDiscordClient:
    async def test_member_role_ids_ok_404_and_errors(self) -> None:
        assert await _discord(lambda r: _resp(200, {"roles": [1, "2"]})).get_member_role_ids(
            guild_id="g", user_id="u"
        ) == {"1", "2"}
        assert (
            await _discord(lambda r: _resp(404)).get_member_role_ids(guild_id="g", user_id="u")
            is None
        )
        with pytest.raises(svc.DiscordSyncError, match="403"):
            await _discord(lambda r: _resp(403)).get_member_role_ids(guild_id="g", user_id="u")
        with pytest.raises(svc.DiscordSyncError, match="member lookup failed"):
            await _discord(_boom).get_member_role_ids(guild_id="g", user_id="u")

    async def test_add_role_uses_put_and_remove_uses_delete(self) -> None:
        verbs: list[tuple[str, str]] = []

        def h(req: httpx.Request) -> httpx.Response:
            verbs.append((req.method, req.url.path))
            assert req.headers["authorization"] == "Bot bot-tok"
            return httpx.Response(204)

        c = _discord(h)
        assert await c.add_role(guild_id="g", user_id="u", role_id="r") is True
        assert await c.remove_role(guild_id="g", user_id="u", role_id="r") is True
        assert verbs == [
            ("PUT", "/guilds/g/members/u/roles/r"),
            ("DELETE", "/guilds/g/members/u/roles/r"),
        ]

    async def test_manage_role_404_returns_false_and_errors_raise(self) -> None:
        added = await _discord(lambda r: _resp(404)).add_role(
            guild_id="g", user_id="u", role_id="r"
        )
        assert added is False
        with pytest.raises(svc.DiscordSyncError, match="role add failed"):
            await _discord(_boom).add_role(guild_id="g", user_id="u", role_id="r")
        with pytest.raises(svc.DiscordSyncError, match="role remove failed"):
            await _discord(_boom).remove_role(guild_id="g", user_id="u", role_id="r")
        with pytest.raises(svc.DiscordSyncError, match="429"):
            await _discord(lambda r: _resp(429)).remove_role(guild_id="g", user_id="u", role_id="r")

    async def test_list_members_skips_rows_without_user_id(self) -> None:
        members = await _discord(
            lambda r: _resp(200, [{"user": {}, "roles": ["1"]}, {"user": {"id": 9}, "roles": [5]}])
        ).list_guild_members(guild_id="g")
        assert [(m.user_id, m.role_ids) for m in members] == [("9", frozenset({"5"}))]


class TestLoopPrevention:
    async def test_discord_to_platform_never_calls_discord_write_api(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _t, _g = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id, direction="bidirectional")
        _link_identity(
            dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_PLATFORM_USER
        )
        _add_community_member(dal, community_id=community_id, hub_user_id=1, role="vip")
        client = _FakeDiscordClient(
            {},
            guild_members=[
                svc.DiscordGuildMember(
                    user_id=_DISCORD_PLATFORM_USER, role_ids=frozenset({_MEMBER_ROLE_DISCORD})
                )
            ],
        )
        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda token: client,
        )
        assert result.community_roles_applied == 1
        assert client.add_calls == []
        assert client.remove_calls == []

    async def test_twitch_to_discord_never_writes_community_member_role(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _t, _g = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        _link_identity(dal, hub_user_id=1, platform="twitch", platform_user_id=_TWITCH_SUB_USER)
        _link_identity(dal, hub_user_id=1, platform="discord", platform_user_id=_DISCORD_SUB_USER)
        _add_community_member(dal, community_id=community_id, hub_user_id=1, role="vip")
        discord = _FakeDiscordClient({_DISCORD_SUB_USER: set()})
        result = await svc.reconcile_pairing(
            dal,
            pairing,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=_FakeTwitchClient(tiers={_TWITCH_SUB_USER: 1}),
            make_discord_client=lambda token: discord,
        )
        assert result.error is None
        assert result.roles_added >= 1
        assert _get_member_role(dal, community_id=community_id, hub_user_id=1) == "vip"


class TestPlatformDirectionFailClosed:
    async def test_no_op_when_wrong_direction(self, bar_citizen_db: Any) -> None:
        dal, community_id, _t, _g = bar_citizen_db
        pairing = _make_pairing(dal, community_id)  # twitch_to_discord
        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda t: _FakeDiscordClient({}),
        )
        assert (result.error, result.community_roles_applied) == (None, 0)

    async def test_flag_off_skips_without_touching_discord(
        self, bar_citizen_db: Any, _flag_on: AsyncMock
    ) -> None:
        _flag_on.return_value = False
        dal, community_id, _t, _g = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        client = _FakeDiscordClient({})
        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda t: client,
        )
        assert result.error is None
        assert client.list_guild_members_calls == 0

    async def test_credential_transport_failure_reports_error_type(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _t, _g = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FailingCredentialResolver(),
            make_discord_client=lambda t: _FakeDiscordClient({}),
        )
        assert result.error == TransportUnavailable.__name__

    async def test_discord_api_error_reports_error_type(self, bar_citizen_db: Any) -> None:
        dal, community_id, _t, _g = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        client = _FakeDiscordClient({})
        client.list_guild_members = AsyncMock(side_effect=svc.DiscordSyncError("x"))  # type: ignore[method-assign]
        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda t: client,
        )
        assert result.error == "DiscordSyncError"

    async def test_unexpected_exception_is_contained(self, bar_citizen_db: Any) -> None:
        dal, community_id, _t, _g = bar_citizen_db
        pairing = _make_platform_pairing(dal, community_id)
        client = _FakeDiscordClient({})
        client.list_guild_members = AsyncMock(side_effect=RuntimeError("bug"))  # type: ignore[method-assign]
        result = await svc.reconcile_pairing_discord_to_platform(
            dal,
            pairing,
            credential_resolver=_FakeCredentialResolver(),
            make_discord_client=lambda t: client,
        )
        assert result.error == "unexpected_error"


class TestBatchAndMain:
    async def test_unknown_direction_is_counted_not_dropped(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, community_id, _t, _g = bar_citizen_db
        pairing = _make_pairing(dal, community_id)
        weird = SimpleNamespace(id=pairing.id, direction="sideways")
        monkeypatch.setattr(svc, "_list_enabled_pairings", lambda d: [weird])
        summary = await svc.run_role_sync_reconcile_batch(
            dal,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FakeCredentialResolver(),
            twitch_client=_FakeTwitchClient(),
        )
        assert summary.pairings_examined == 1
        assert summary.pairings_skipped_wrong_direction == 1
        assert summary.pairings_synced == 0

    async def test_batch_default_clients_and_credential_failure_counts_failed(
        self, bar_citizen_db: Any
    ) -> None:
        dal, community_id, _t, _g = bar_citizen_db
        _make_pairing(dal, community_id, direction="bidirectional")
        summary = await svc.run_role_sync_reconcile_batch(
            dal,
            get_broadcaster_user_token=_always_token,
            credential_resolver=_FailingCredentialResolver(),
            twitch_client=_FakeTwitchClient(),
        )
        assert summary.pairings_examined == 1
        assert summary.pairings_failed == 1
        assert summary.pairings_synced == 0

    async def test_main_prints_denominators(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_dal = object()

        async def _fake_install_dal() -> Any:
            return SimpleNamespace(dal=fake_dal)

        async def _fake_batch(dal: Any, **kw: Any) -> svc.ReconcileSummary:
            assert dal is fake_dal
            assert await kw["get_broadcaster_user_token"](1) == "tok"
            return svc.ReconcileSummary(pairings_examined=3, pairings_synced=2, pairings_failed=1)

        async def _fake_tokens(install_dal: Any, community_id: int, platform: str) -> Any:
            assert platform == "twitch"
            return SimpleNamespace(access_token="tok")  # noqa: S106

        monkeypatch.setattr(svc, "_build_install_dal", _fake_install_dal)
        monkeypatch.setattr(svc, "run_role_sync_reconcile_batch", _fake_batch)
        import services.community_connections as cc

        monkeypatch.setattr(cc, "get_decrypted_tokens", _fake_tokens)
        assert await svc.main() == 0
        out = capsys.readouterr().out
        assert "pairings_examined=3" in out
        assert "pairings_failed=1" in out
