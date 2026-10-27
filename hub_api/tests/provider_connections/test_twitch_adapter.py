"""The Twitch adapter against the in-process fake Twitch (real guard, real HTTP code, fake socket)."""

from __future__ import annotations

import logging
import socket
from datetime import timedelta
from typing import Any

import httpx
import pytest

from services.oauth_providers import ProviderNotConfigured
from services.provider_connections.adapter import Credentials, ProviderAdapter
from services.provider_connections.errors import (
    CredentialsRejected,
    InsufficientScopes,
    ProviderUnavailable,
    RefreshNotConfigured,
    RefreshRejected,
    ResourceUnresolvable,
)
from services.provider_connections.twitch import (
    CAPABILITY_ACTION_RELAY,
    CAPABILITY_CHAT_INGEST,
    TWITCH_SPEC,
    TwitchAdapter,
)
from tests.provider_connections.conftest import (
    BOT,
    CHANNEL_ID,
    CHANNEL_LOGIN,
    FakeClock,
)
from tests.provider_connections.fake_twitch import (
    APP_CLIENT_ID,
    APP_CLIENT_SECRET,
    OTHER_APP_CLIENT_ID,
    FakeAccount,
    FakeTwitch,
)


@pytest.fixture
def adapter(fake_twitch: FakeTwitch, clock: FakeClock) -> TwitchAdapter:
    return TwitchAdapter(
        transport=fake_twitch.transport,
        app_credentials=lambda: (APP_CLIENT_ID, APP_CLIENT_SECRET),
        clock=clock,
    )


def test_adapter_satisfies_the_provider_contract(adapter: TwitchAdapter) -> None:
    assert isinstance(adapter, ProviderAdapter)
    assert adapter.spec is TWITCH_SPEC
    assert TWITCH_SPEC.provider == TWITCH_SPEC.platform == "twitch"
    assert TWITCH_SPEC.resource_type == "twitch_channel"


class TestNormalize:
    @pytest.mark.parametrize("prefix", ["oauth:", "OAuth:", "OAUTH:"])
    def test_irc_style_prefix_is_stripped(self, adapter: TwitchAdapter, prefix: str) -> None:
        result = adapter.normalize_credentials(Credentials(f"{prefix}abc123", "r"))
        assert result.access_token == "abc123"
        assert result.refresh_token == "r"

    def test_plain_token_is_unchanged(self, adapter: TwitchAdapter) -> None:
        assert adapter.normalize_credentials(Credentials("abc123")).access_token == "abc123"


class TestCapabilities:
    def test_scopes_map_to_capabilities(self, adapter: TwitchAdapter) -> None:
        assert adapter.granted_capabilities(["chat:read"]) == {CAPABILITY_CHAT_INGEST}
        assert adapter.granted_capabilities(["chat:edit"]) == {CAPABILITY_ACTION_RELAY}
        assert adapter.granted_capabilities(["chat:read", "chat:edit", "x"]) == {
            CAPABILITY_CHAT_INGEST,
            CAPABILITY_ACTION_RELAY,
        }
        assert adapter.granted_capabilities([]) == frozenset()


class TestValidate:
    async def test_reports_owner_app_scopes_and_expiry(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch, clock: FakeClock
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT, expires_in=14000)
        account = await adapter.validate_credentials(Credentials(access))
        assert account.account_id == BOT.user_id
        assert account.account_label == BOT.login
        assert account.app_id == APP_CLIENT_ID
        assert account.scopes == ("chat:read", "chat:edit")
        assert account.expires_at == clock.now + timedelta(seconds=14000)

    @pytest.mark.parametrize("expires_in", [0, -5])
    async def test_non_positive_lifetime_means_no_expiry(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch, expires_in: int
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT, expires_in=expires_in)
        assert (await adapter.validate_credentials(Credentials(access))).expires_at is None

    async def test_unknown_token_is_rejected(self, adapter: TwitchAdapter) -> None:
        with pytest.raises(CredentialsRejected):
            await adapter.validate_credentials(Credentials("not-a-real-token"))

    async def test_app_access_token_has_no_user_and_is_rejected(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT, user_token=False)
        with pytest.raises(CredentialsRejected, match="user access token"):
            await adapter.validate_credentials(Credentials(access))

    async def test_missing_chat_read_names_the_scope(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT, scopes=["chat:edit"])
        with pytest.raises(InsufficientScopes, match="chat:read"):
            await adapter.validate_credentials(Credentials(access))

    async def test_chat_read_alone_is_enough(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT, scopes=["chat:read"])
        account = await adapter.validate_credentials(Credentials(access))
        assert adapter.granted_capabilities(account.scopes) == {CAPABILITY_CHAT_INGEST}

    @pytest.mark.parametrize("status", [403, 429, 500, 503])
    async def test_unexpected_status_is_provider_unavailable(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch, status: int
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        fake_twitch.status_override["validate"] = status
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials(access))

    async def test_connection_failure_is_provider_unavailable(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        fake_twitch.down = True
        with pytest.raises(ProviderUnavailable, match="could not be reached"):
            await adapter.validate_credentials(Credentials(access))

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            b'"str"',
            b'{"client_id": "x", "login": "b", "user_id": "1", "scopes": ["chat:read"]',
        ],
    )
    async def test_malformed_body_is_provider_unavailable(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch, body: bytes
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        fake_twitch.raw_body_override["validate"] = body
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials(access))

    async def test_missing_client_id_is_provider_unavailable(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        fake_twitch.raw_body_override["validate"] = (
            b'{"login": "b", "user_id": "1", "scopes": ["chat:read"]}'
        )
        with pytest.raises(ProviderUnavailable, match="unexpected"):
            await adapter.validate_credentials(Credentials(access))

    async def test_non_list_scopes_means_no_scopes(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        fake_twitch.raw_body_override["validate"] = (
            b'{"client_id": "c", "login": "b", "user_id": "1", "scopes": "chat:read"}'
        )
        with pytest.raises(InsufficientScopes):
            await adapter.validate_credentials(Credentials(access))

    async def test_oversized_response_is_refused(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        fake_twitch.raw_body_override["validate"] = b"x" * 300_000
        with pytest.raises(ProviderUnavailable, match="large"):
            await adapter.validate_credentials(Credentials(access))

    async def test_redirects_are_never_followed(
        self, fake_twitch: FakeTwitch, clock: FakeClock
    ) -> None:
        followed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            followed.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

        adapter = TwitchAdapter(transport=httpx.MockTransport(handler), clock=clock)
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials("tok"))
        assert followed == ["https://id.twitch.tv/oauth2/validate"]

    async def test_the_token_travels_in_a_header_never_in_a_url(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, refresh = fake_twitch.mint(account=BOT)
        fake_fix = FakeAccount(user_id="9", login="x")
        del fake_fix
        account = await adapter.validate_credentials(Credentials(access))
        await adapter.resolve_resource(
            Credentials(access), account, {"channel_login": CHANNEL_LOGIN}
        )
        assert refresh is not None
        await adapter.refresh(refresh, {"client_id": APP_CLIENT_ID})
        assert fake_twitch.urls  # the scan below must examine something
        assert not any(access in url or refresh in url for url in fake_twitch.urls)


class TestResolve:
    async def test_channel_login_resolves_to_the_stable_user_id(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        creds = Credentials(access)
        account = await adapter.validate_credentials(creds)
        resource = await adapter.resolve_resource(creds, account, {"channel_login": CHANNEL_LOGIN})
        assert resource.resource_id == CHANNEL_ID
        assert resource.facts == {
            "channel_login": CHANNEL_LOGIN,
            "channel_id": CHANNEL_ID,
            "bot_login": BOT.login,
            "bot_user_id": BOT.user_id,
            "client_id": APP_CLIENT_ID,
        }

    async def test_unknown_channel(self, adapter: TwitchAdapter, fake_twitch: FakeTwitch) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        creds = Credentials(access)
        account = await adapter.validate_credentials(creds)
        with pytest.raises(ResourceUnresolvable, match="does not exist"):
            await adapter.resolve_resource(creds, account, {"channel_login": "nosuchchannel"})

    async def test_response_for_a_different_login_is_not_accepted(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        creds = Credentials(access)
        account = await adapter.validate_credentials(creds)
        fake_twitch.raw_body_override["helix"] = b'{"data": [{"id": "1", "login": "someoneelse"}]}'
        with pytest.raises(ResourceUnresolvable):
            await adapter.resolve_resource(creds, account, {"channel_login": CHANNEL_LOGIN})

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (401, CredentialsRejected),
            (400, ResourceUnresolvable),
            (429, ProviderUnavailable),
            (500, ProviderUnavailable),
        ],
    )
    async def test_helix_status_mapping(
        self,
        adapter: TwitchAdapter,
        fake_twitch: FakeTwitch,
        status: int,
        error: type[Exception],
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        creds = Credentials(access)
        account = await adapter.validate_credentials(creds)
        fake_twitch.status_override["helix"] = status
        with pytest.raises(error):
            await adapter.resolve_resource(creds, account, {"channel_login": CHANNEL_LOGIN})

    async def test_data_without_a_string_id_is_unresolvable(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        access, _ = fake_twitch.mint(account=BOT)
        creds = Credentials(access)
        account = await adapter.validate_credentials(creds)
        fake_twitch.raw_body_override["helix"] = b'{"data": [{"id": 7, "login": "penguinzplays"}]}'
        with pytest.raises(ResourceUnresolvable):
            await adapter.resolve_resource(creds, account, {"channel_login": CHANNEL_LOGIN})


class TestRefresh:
    async def test_success_rotates_both_tokens(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch, clock: FakeClock
    ) -> None:
        _, refresh = fake_twitch.mint(account=BOT)
        assert refresh is not None
        new = await adapter.refresh(refresh, {"client_id": APP_CLIENT_ID})
        assert new.access_token and new.refresh_token and new.refresh_token != refresh
        assert new.expires_at == clock.now + timedelta(seconds=14400)
        assert new.scopes == ("chat:read", "chat:edit")
        # Single use: the spent token is dead, the new one works.
        with pytest.raises(RefreshRejected):
            await adapter.refresh(refresh, {"client_id": APP_CLIENT_ID})
        again = await adapter.refresh(new.refresh_token, {"client_id": APP_CLIENT_ID})
        assert again.access_token != new.access_token

    @pytest.mark.parametrize("status", [400, 401])
    async def test_rejection(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch, status: int
    ) -> None:
        _, refresh = fake_twitch.mint(account=BOT)
        assert refresh is not None
        fake_twitch.status_override["token"] = status
        with pytest.raises(RefreshRejected):
            await adapter.refresh(refresh, {})

    async def test_transient_failure_is_provider_unavailable_and_not_a_rejection(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        _, refresh = fake_twitch.mint(account=BOT)
        assert refresh is not None
        fake_twitch.status_override["token"] = 503
        with pytest.raises(ProviderUnavailable):
            await adapter.refresh(refresh, {})

    async def test_no_platform_app_configured(
        self, fake_twitch: FakeTwitch, clock: FakeClock
    ) -> None:
        def unset() -> tuple[str, str]:
            raise ProviderNotConfigured("twitch: missing TWITCH_CLIENT_ID")

        adapter = TwitchAdapter(transport=fake_twitch.transport, app_credentials=unset, clock=clock)
        _, refresh = fake_twitch.mint(account=BOT)
        assert refresh is not None
        with pytest.raises(RefreshNotConfigured):
            await adapter.refresh(refresh, {})
        assert fake_twitch.refresh_calls == 0  # no request was spent on a refresh that cannot work

    async def test_token_from_another_twitch_app_is_not_sent_to_twitch(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        _, refresh = fake_twitch.mint(account=BOT, client_id=OTHER_APP_CLIENT_ID)
        assert refresh is not None
        with pytest.raises(RefreshNotConfigured, match="different Twitch application"):
            await adapter.refresh(refresh, {"client_id": OTHER_APP_CLIENT_ID})
        assert fake_twitch.refresh_calls == 0

    async def test_response_without_access_token_is_provider_unavailable(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        _, refresh = fake_twitch.mint(account=BOT)
        assert refresh is not None
        fake_twitch.raw_body_override["token"] = b'{"refresh_token": "r"}'
        with pytest.raises(ProviderUnavailable, match="unexpected"):
            await adapter.refresh(refresh, {})

    async def test_response_without_a_new_refresh_token_keeps_none(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch
    ) -> None:
        _, refresh = fake_twitch.mint(account=BOT)
        assert refresh is not None
        fake_twitch.raw_body_override["token"] = b'{"access_token": "a", "scope": "chat:read"}'
        new = await adapter.refresh(refresh, {})
        assert new.refresh_token is None
        assert new.scopes == ()
        assert new.expires_at is None


class TestGuardAndLogging:
    async def test_the_ssrf_guard_runs_before_every_request(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def private(host: str, *args: Any, **kwargs: Any) -> Any:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

        monkeypatch.setattr("services.url_guard.socket.getaddrinfo", private)
        access, _ = fake_twitch.mint(account=BOT)
        with pytest.raises(ProviderUnavailable, match="network guard"):
            await adapter.validate_credentials(Credentials(access))
        assert fake_twitch.calls == []  # nothing reached the socket

    async def test_no_log_line_carries_a_token_or_an_account_name(
        self, adapter: TwitchAdapter, fake_twitch: FakeTwitch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        access, refresh = fake_twitch.mint(account=BOT)
        assert refresh is not None
        creds = Credentials(access, refresh)
        account = await adapter.validate_credentials(creds)
        await adapter.resolve_resource(creds, account, {"channel_login": CHANNEL_LOGIN})
        new = await adapter.refresh(refresh, {"client_id": APP_CLIENT_ID})
        fake_twitch.status_override["validate"] = 500
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(creds)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert caplog.records  # the scan below must examine something
        for secret in (access, refresh, new.access_token, new.refresh_token, APP_CLIENT_SECRET):
            assert secret and secret not in text
        assert BOT.login not in text and CHANNEL_LOGIN not in text

    def test_credentials_repr_hides_secrets(self) -> None:
        text = repr(Credentials("super-secret-access", "super-secret-refresh"))
        assert "super-secret" not in text
