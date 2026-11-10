"""Tests for `services/oauth_providers.py` -- issue #320 chunk C2 (provider registry + exchange).

Same mocked-transport approach as `tests/test_music_providers_youtube_oauth.py`:
`httpx.MockTransport` swapped in for `httpx.AsyncClient` via monkeypatch, so
request building/param encoding/JSON parsing all run for real, with zero
real network I/O. `services.oauth_providers.validate_outbound_url` is
replaced with a fast no-op pass-through by an autouse fixture -- the real
guard resolves DNS via `socket.getaddrinfo` in a thread, which would make
every test dependent on network availability for no security benefit
(provider endpoints here are fixed constants, not attacker input); guard
*integration* (a rejection propagates as `OAuthExchangeError`, and never
crashes `fetch_account_label`) is exercised directly in
`TestSSRFGuardIntegration` by overriding the no-op back to a raising stub.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from services import oauth_providers as op
from services.errors import bad_request

_RealAsyncClient = httpx.AsyncClient


def _client_factory(transport: httpx.MockTransport) -> Callable[..., httpx.AsyncClient]:
    """Build a replacement for `httpx.AsyncClient` that always uses `transport`."""

    def factory(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=transport)

    return factory


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(httpx.MockTransport(handler)))


@pytest.fixture(autouse=True)
def _no_real_ssrf_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real DNS-resolving SSRF guard by default -- see module docstring."""

    async def _pass_through(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
        return url

    monkeypatch.setattr(op, "validate_outbound_url", _pass_through)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No provider ever has real credentials unless a test sets them explicitly."""
    for spec in op.PROVIDERS.values():
        monkeypatch.delenv(spec.client_id_env, raising=False)
        monkeypatch.delenv(spec.client_secret_env, raising=False)


def _set_creds(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[str, str]:
    spec = op.get_provider(name)
    client_id, client_secret = f"{name}-client-id", f"{name}-client-secret"  # noqa: S105
    monkeypatch.setenv(spec.client_id_env, client_id)
    monkeypatch.setenv(spec.client_secret_env, client_secret)
    return client_id, client_secret


class TestGetProvider:
    """`get_provider()` -- lookup + the `KeyError -> ValueError` translation."""

    @pytest.mark.parametrize("name", ["youtube", "spotify", "twitch", "discord", "kick", "slack"])
    def test_known_providers_resolve(self, name: str) -> None:
        spec = op.get_provider(name)
        assert spec.name == name

    def test_unknown_provider_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unsupported provider"):
            op.get_provider("myspace")


class TestClientCredentials:
    """`client_credentials()` -- env resolution + fail-closed on missing/empty."""

    def test_returns_configured_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client_id, client_secret = _set_creds(monkeypatch, "discord")
        spec = op.get_provider("discord")
        assert op.client_credentials(spec) == (client_id, client_secret)

    def test_missing_both_raises_provider_not_configured(self) -> None:
        spec = op.get_provider("discord")
        with pytest.raises(op.ProviderNotConfigured):
            op.client_credentials(spec)

    def test_missing_secret_only_raises_provider_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = op.get_provider("discord")
        monkeypatch.setenv(spec.client_id_env, "id-only")
        with pytest.raises(op.ProviderNotConfigured):
            op.client_credentials(spec)

    def test_empty_string_treated_as_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = op.get_provider("discord")
        monkeypatch.setenv(spec.client_id_env, "")
        monkeypatch.setenv(spec.client_secret_env, "secret")
        with pytest.raises(op.ProviderNotConfigured):
            op.client_credentials(spec)


class TestMakePkcePair:
    """`make_pkce_pair()` -- RFC 7636 S256 correctness."""

    def test_challenge_is_sha256_s256_of_verifier(self) -> None:
        verifier, challenge = op.make_pkce_pair()
        expected_digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected_challenge = base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode()
        assert challenge == expected_challenge

    def test_verifier_has_no_padding_and_is_url_safe(self) -> None:
        verifier, challenge = op.make_pkce_pair()
        assert "=" not in verifier
        assert "=" not in challenge
        assert 43 <= len(verifier) <= 128

    def test_pairs_are_unique_across_calls(self) -> None:
        first_verifier, _ = op.make_pkce_pair()
        second_verifier, _ = op.make_pkce_pair()
        assert first_verifier != second_verifier


class TestBuildAuthorizeUrl:
    """`build_authorize_url()` -- one test per provider's params, plus Kick's PKCE requirement."""

    @pytest.mark.parametrize("name", ["twitch", "discord", "slack"])
    def test_non_pkce_provider_url_has_core_params(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        client_id, _ = _set_creds(monkeypatch, name)
        spec = op.get_provider(name)

        url = op.build_authorize_url(name, redirect_uri="https://hub.example/cb", state="state-123")

        parsed = httpx.URL(url)
        assert str(parsed.copy_with(query=None)) == spec.authorize_url
        assert parsed.params["client_id"] == client_id
        assert parsed.params["redirect_uri"] == "https://hub.example/cb"
        assert parsed.params["response_type"] == "code"
        assert parsed.params["state"] == "state-123"
        assert parsed.params["scope"] == " ".join(spec.scopes)
        assert "code_challenge" not in parsed.params
        for key, value in spec.extra_authorize_params.items():
            assert parsed.params[key] == value

    def test_youtube_extra_params_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_creds(monkeypatch, "youtube")
        _, challenge = op.make_pkce_pair()
        url = op.build_authorize_url(
            "youtube", redirect_uri="https://hub.example/cb", state="s", code_challenge=challenge
        )
        parsed = httpx.URL(url)
        assert parsed.params["access_type"] == "offline"
        assert parsed.params["prompt"] == "consent"
        assert parsed.params["include_granted_scopes"] == "true"

    def test_pkce_provider_set_is_pinned(self) -> None:
        """Every provider documented to support PKCE uses it; the rest are explicit non-PKCE."""
        pkce = {name for name, spec in op.PROVIDERS.items() if spec.uses_pkce}
        assert pkce == {"youtube", "spotify", "kick"}

    @pytest.mark.parametrize("name", ["youtube", "spotify", "kick"])
    def test_pkce_provider_sends_s256_challenge_derived_from_the_verifier(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        import base64
        import hashlib

        _set_creds(monkeypatch, name)
        verifier, challenge = op.make_pkce_pair()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        url = op.build_authorize_url(
            name, redirect_uri="https://hub.example/cb", state="s", code_challenge=challenge
        )
        parsed = httpx.URL(url)
        assert parsed.params["code_challenge"] == challenge
        assert parsed.params["code_challenge_method"] == "S256"
        assert verifier not in url  # only the derived challenge ever travels via the browser

    @pytest.mark.parametrize("name", ["youtube", "spotify"])
    def test_pkce_provider_without_challenge_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        _set_creds(monkeypatch, name)
        with pytest.raises(ValueError, match="code_challenge"):
            op.build_authorize_url(name, redirect_uri="https://hub.example/cb", state="s")

    def test_kick_with_code_challenge_sets_pkce_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "kick")
        _, challenge = op.make_pkce_pair()

        url = op.build_authorize_url(
            "kick",
            redirect_uri="https://hub.example/cb",
            state="s",
            code_challenge=challenge,
        )

        parsed = httpx.URL(url)
        assert parsed.params["code_challenge"] == challenge
        assert parsed.params["code_challenge_method"] == "S256"

    def test_kick_without_code_challenge_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_creds(monkeypatch, "kick")
        with pytest.raises(ValueError, match="code_challenge"):
            op.build_authorize_url("kick", redirect_uri="https://hub.example/cb", state="s")

    def test_unconfigured_provider_raises_provider_not_configured(self) -> None:
        with pytest.raises(op.ProviderNotConfigured):
            op.build_authorize_url("discord", redirect_uri="https://hub.example/cb", state="s")

    def test_unknown_provider_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unsupported provider"):
            op.build_authorize_url("myspace", redirect_uri="https://hub.example/cb", state="s")


def _token_handler(
    *, host: str, response_json: dict[str, Any], status_code: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == host
        return httpx.Response(status_code, json=response_json)

    return handler


class TestExchangeCodeSuccess:
    """`exchange_code()` -- happy path per provider, covering both `token_auth` modes."""

    async def test_body_auth_provider_sends_client_id_and_secret_in_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_id, client_secret = _set_creds(monkeypatch, "discord")
        seen_body: dict[str, str] = {}
        seen_auth_header = "unset"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_auth_header
            seen_auth_header = request.headers.get("Authorization", "unset")
            seen_body.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(
                200,
                json={
                    "access_token": "discord-access",
                    "refresh_token": "discord-refresh",
                    "expires_in": 604800,
                    "scope": "identify guilds",
                    "token_type": "Bearer",
                },
            )

        _install_transport(monkeypatch, handler)

        result = await op.exchange_code(
            "discord", code="auth-code", redirect_uri="https://hub.example/cb"
        )

        assert result.access_token == "discord-access"
        assert result.refresh_token == "discord-refresh"
        assert result.expires_in == 604800
        assert result.scopes == ["identify", "guilds"]
        assert result.token_type == "Bearer"
        assert seen_body["client_id"] == client_id
        assert seen_body["client_secret"] == client_secret
        assert seen_body["grant_type"] == "authorization_code"
        assert seen_body["code"] == "auth-code"
        assert seen_auth_header == "unset"

    async def test_basic_auth_provider_sends_authorization_header_not_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_id, client_secret = _set_creds(monkeypatch, "spotify")
        seen_body: dict[str, str] = {}
        seen_auth_header = "unset"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_auth_header
            seen_auth_header = request.headers.get("Authorization", "unset")
            seen_body.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(
                200,
                json={"access_token": "spotify-access", "expires_in": 3600, "token_type": "Bearer"},
            )

        _install_transport(monkeypatch, handler)

        result = await op.exchange_code(
            "spotify", code="auth-code", redirect_uri="https://hub.example/cb"
        )

        expected_basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        assert seen_auth_header == f"Basic {expected_basic}"
        assert "client_id" not in seen_body
        assert "client_secret" not in seen_body
        assert result.access_token == "spotify-access"
        assert result.refresh_token is None
        # spotify's response has no "scope" field -- falls back to the requested scopes.
        assert result.scopes == list(op.get_provider("spotify").scopes)

    @pytest.mark.parametrize("name", ["youtube", "spotify"])
    async def test_spotify_and_youtube_exchange_sends_the_verifier(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        _set_creds(monkeypatch, name)
        seen_body: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_body.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer"})

        _install_transport(monkeypatch, handler)
        await op.exchange_code(
            name, code="c", redirect_uri="https://hub.example/cb", code_verifier="v" * 43
        )
        assert seen_body["code_verifier"] == "v" * 43
        assert seen_body["grant_type"] == "authorization_code"

    async def test_kick_pkce_code_verifier_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_creds(monkeypatch, "kick")
        seen_body: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_body.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"access_token": "kick-access", "token_type": "Bearer"})

        _install_transport(monkeypatch, handler)

        await op.exchange_code(
            "kick",
            code="auth-code",
            redirect_uri="https://hub.example/cb",
            code_verifier="verifier-abc",
        )

        assert seen_body["code_verifier"] == "verifier-abc"

    async def test_slack_ok_true_maps_top_level_access_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "slack")
        _install_transport(
            monkeypatch,
            _token_handler(
                host="slack.com",
                response_json={
                    "ok": True,
                    "access_token": "slack-bot-token",
                    "scope": "chat:write,channels:read",
                    "token_type": "bot",
                    "team": {"id": "T1", "name": "Test Team"},
                },
            ),
        )

        result = await op.exchange_code(
            "slack", code="auth-code", redirect_uri="https://hub.example/cb"
        )

        assert result.access_token == "slack-bot-token"
        assert result.scopes == ["chat:write", "channels:read"]


class TestExchangeCodeFailure:
    """`exchange_code()` -- non-2xx, malformed JSON, Slack `ok: false`, transport errors."""

    async def test_non_2xx_raises_oauth_exchange_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "discord")
        _install_transport(
            monkeypatch, _token_handler(host="discord.com", response_json={}, status_code=401)
        )

        with pytest.raises(op.OAuthExchangeError, match="401"):
            await op.exchange_code(
                "discord", code="bad-code", redirect_uri="https://hub.example/cb"
            )

    async def test_response_missing_access_token_raises_oauth_exchange_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "discord")
        _install_transport(
            monkeypatch,
            _token_handler(host="discord.com", response_json={"token_type": "Bearer"}),
        )

        with pytest.raises(op.OAuthExchangeError, match="missing access_token"):
            await op.exchange_code(
                "discord", code="auth-code", redirect_uri="https://hub.example/cb"
            )

    async def test_scope_returned_as_list_is_mapped_to_str_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "kick")
        _install_transport(
            monkeypatch,
            _token_handler(
                host="id.kick.com",
                response_json={
                    "access_token": "kick-access",
                    "token_type": "Bearer",
                    "scope": ["user:read", "channel:read"],
                },
            ),
        )

        result = await op.exchange_code(
            "kick", code="auth-code", redirect_uri="https://hub.example/cb"
        )

        assert result.scopes == ["user:read", "channel:read"]

    async def test_malformed_json_raises_oauth_exchange_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "discord")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        _install_transport(monkeypatch, handler)

        with pytest.raises(op.OAuthExchangeError, match="malformed"):
            await op.exchange_code(
                "discord", code="auth-code", redirect_uri="https://hub.example/cb"
            )

    async def test_non_object_json_raises_oauth_exchange_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "discord")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["not", "an", "object"])

        _install_transport(monkeypatch, handler)

        with pytest.raises(op.OAuthExchangeError, match="malformed"):
            await op.exchange_code(
                "discord", code="auth-code", redirect_uri="https://hub.example/cb"
            )

    async def test_slack_ok_false_raises_oauth_exchange_error_without_leaking_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "slack")
        _install_transport(
            monkeypatch,
            _token_handler(
                host="slack.com",
                # A real Slack `ok: false` response never carries a token, but even
                # if a provider misbehaved and did, the raised message must not
                # echo it back -- this response includes one specifically to prove
                # that.
                response_json={
                    "ok": False,
                    "error": "invalid_code",
                    "access_token": "should-never-appear-in-error-xyz",
                },
            ),
        )

        with pytest.raises(op.OAuthExchangeError, match="invalid_code") as exc_info:
            await op.exchange_code("slack", code="bad-code", redirect_uri="https://hub.example/cb")
        assert "should-never-appear-in-error-xyz" not in str(exc_info.value)

    async def test_transport_failure_raises_oauth_exchange_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "discord")

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        _install_transport(monkeypatch, handler)

        with pytest.raises(op.OAuthExchangeError, match="request failed"):
            await op.exchange_code(
                "discord", code="auth-code", redirect_uri="https://hub.example/cb"
            )

    async def test_missing_client_credentials_raises_provider_not_configured(self) -> None:
        with pytest.raises(op.ProviderNotConfigured):
            await op.exchange_code(
                "discord", code="auth-code", redirect_uri="https://hub.example/cb"
            )

    async def test_unknown_provider_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="unsupported provider"):
            await op.exchange_code(
                "myspace", code="auth-code", redirect_uri="https://hub.example/cb"
            )


class TestRefreshAccessToken:
    """`refresh_access_token()` -- with and without a provider-issued new refresh token."""

    async def test_provider_returns_new_refresh_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "discord")
        _install_transport(
            monkeypatch,
            _token_handler(
                host="discord.com",
                response_json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            ),
        )

        result = await op.refresh_access_token("discord", refresh_token="old-refresh")

        assert result.access_token == "new-access"
        assert result.refresh_token == "new-refresh"

    async def test_provider_omits_refresh_token_yields_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "spotify")
        _install_transport(
            monkeypatch,
            _token_handler(
                host="accounts.spotify.com",
                response_json={"access_token": "new-access", "expires_in": 3600},
            ),
        )

        result = await op.refresh_access_token("spotify", refresh_token="old-refresh")

        assert result.access_token == "new-access"
        assert result.refresh_token is None

    async def test_sends_grant_type_refresh_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_creds(monkeypatch, "discord")
        seen_body: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_body.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"access_token": "new-access", "token_type": "Bearer"})

        _install_transport(monkeypatch, handler)

        await op.refresh_access_token("discord", refresh_token="old-refresh")

        assert seen_body["grant_type"] == "refresh_token"
        assert seen_body["refresh_token"] == "old-refresh"

    async def test_non_2xx_raises_oauth_exchange_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "discord")
        _install_transport(
            monkeypatch, _token_handler(host="discord.com", response_json={}, status_code=400)
        )

        with pytest.raises(op.OAuthExchangeError):
            await op.refresh_access_token("discord", refresh_token="old-refresh")


class TestFetchAccountLabel:
    """`fetch_account_label()` -- one success case per provider, plus never-raises failures."""

    async def test_youtube_label_from_channel_title(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(
                200,
                json={"items": [{"snippet": {"title": "My Channel"}}]},
            ),
        )
        assert await op.fetch_account_label("youtube", "tok") == "My Channel"

    async def test_spotify_label_from_display_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json={"display_name": "Jane"})
        )
        assert await op.fetch_account_label("spotify", "tok") == "Jane"

    async def test_twitch_label_from_login_and_sends_client_id_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "twitch")
        seen_client_id_header = "unset"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_client_id_header
            seen_client_id_header = request.headers.get("Client-Id", "unset")
            return httpx.Response(200, json={"data": [{"login": "streamerlogin"}]})

        _install_transport(monkeypatch, handler)

        assert await op.fetch_account_label("twitch", "tok") == "streamerlogin"
        assert seen_client_id_header == "twitch-client-id"

    async def test_discord_label_from_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json={"username": "someuser"})
        )
        assert await op.fetch_account_label("discord", "tok") == "someuser"

    async def test_kick_label_from_data_zero_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(200, json={"data": [{"name": "KickUser"}]}),
        )
        assert await op.fetch_account_label("kick", "tok") == "KickUser"

    async def test_slack_label_from_team(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch,
            lambda request: httpx.Response(200, json={"ok": True, "team": "Test Team"}),
        )
        assert await op.fetch_account_label("slack", "tok") == "Test Team"

    async def test_slack_ok_false_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"ok": False}))
        assert await op.fetch_account_label("slack", "tok") is None

    async def test_non_2xx_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(monkeypatch, lambda request: httpx.Response(500, json={}))
        assert await op.fetch_account_label("discord", "tok") is None

    async def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(monkeypatch, lambda request: httpx.Response(200, content=b"not json"))
        assert await op.fetch_account_label("discord", "tok") is None

    async def test_transport_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        _install_transport(monkeypatch, handler)
        assert await op.fetch_account_label("discord", "tok") is None

    async def test_missing_expected_field_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_transport(monkeypatch, lambda request: httpx.Response(200, json={}))
        assert await op.fetch_account_label("discord", "tok") is None

    async def test_unknown_provider_returns_none(self) -> None:
        assert await op.fetch_account_label("myspace", "tok") is None

    async def test_non_object_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(
            monkeypatch, lambda request: httpx.Response(200, json=["not", "an", "object"])
        )
        assert await op.fetch_account_label("discord", "tok") is None

    async def test_youtube_no_items_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"items": []}))
        assert await op.fetch_account_label("youtube", "tok") is None

    async def test_twitch_no_data_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_creds(monkeypatch, "twitch")
        _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))
        assert await op.fetch_account_label("twitch", "tok") is None

    async def test_kick_no_data_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))
        assert await op.fetch_account_label("kick", "tok") is None


class TestExtractLabelUnknownProvider:
    """`_extract_label`'s trailing `return None` -- unreachable via `fetch_account_label`.

    `fetch_account_label` rejects an unknown provider name before ever
    calling this helper, so this branch is exercised directly as a
    defensive-branch regression guard instead.
    """

    def test_unrecognized_name_returns_none(self) -> None:
        assert op._extract_label("myspace", {"anything": "goes"}) is None


class TestSSRFGuardIntegration:
    """Confirms `validate_outbound_url` is actually invoked, not just importable.

    Fail-first proof (executed, not narrated): temporarily removed the
    `await _guard_url(...)` call from `_post_token` -- `test_guard_rejection_
    on_exchange_becomes_oauth_exchange_error` went red (a `MockTransport`
    request was reached despite the raising guard stub instead of being
    rejected first); reverted, green again.
    """

    async def test_guard_rejection_on_exchange_becomes_oauth_exchange_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_creds(monkeypatch, "discord")

        async def _raising_guard(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
            raise bad_request("URL resolves to a disallowed network address: evil")

        monkeypatch.setattr(op, "validate_outbound_url", _raising_guard)

        transport_reached = False

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal transport_reached
            transport_reached = True
            return httpx.Response(200, json={"access_token": "x", "token_type": "Bearer"})

        _install_transport(monkeypatch, handler)

        with pytest.raises(op.OAuthExchangeError, match="outbound URL blocked"):
            await op.exchange_code(
                "discord", code="auth-code", redirect_uri="https://hub.example/cb"
            )
        assert transport_reached is False

    async def test_guard_rejection_on_fetch_label_is_swallowed_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _raising_guard(url: str, *, allowed_schemes: tuple[str, ...]) -> str:
            raise bad_request("URL resolves to a disallowed network address: evil")

        monkeypatch.setattr(op, "validate_outbound_url", _raising_guard)

        assert await op.fetch_account_label("discord", "tok") is None
