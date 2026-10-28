"""`blueprints/v1/community_connections.py` -- per-community OAuth Connections page (gh-320, C3).

C1 (`services/community_connections.py`)/C2 (`services/oauth_providers.py`)
functions are monkeypatched at this blueprint module's own imported
references (`connections_module.connections_svc.*`/`connections_module.
providers_svc.*`/`connections_module.state_svc.*`) -- same pattern
`test_community_activity.py` uses for `feature_enabled` -- rather than
exercising their real DB/HTTP behavior, which belongs to C1/C2's own test
suites.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey, spiffe_id
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_connections as connections_module
from blueprints.v1.community_connections import connections_bp, connections_internal_bp
from config import HubAPIConfig
from services.community_connections import ConnectionStatus, DecryptedTokens, UnsupportedProvider
from services.oauth_connection_state import StatePayload
from services.oauth_providers import (
    OAuthExchangeError,
    ProviderNotConfigured,
    ProviderSpec,
    TokenResponse,
)

CALLBACK_BASE = "http://localhost:19999"
SERVICE_ID = spiffe_id("alpha", "svc-action")


def _test_hub_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="global",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
        connections_callback_base_url=CALLBACK_BASE,
    )


def _spec(*, uses_pkce: bool = False) -> ProviderSpec:
    return ProviderSpec(
        name="youtube",
        display_name="YouTube",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106 - URL, not a credential
        scopes=("scope-a",),
        extra_authorize_params={},
        uses_pkce=uses_pkce,
        client_id_env="YOUTUBE_CLIENT_ID",
        client_secret_env="YOUTUBE_CLIENT_SECRET",  # noqa: S106 - env var name, not a credential
        token_auth="body",  # noqa: S106 - auth-mode literal, not a credential
    )


@pytest.fixture
def app(community_db: Any) -> Quart:
    dal, _community_id = community_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(connections_bp)
    quart_app.register_blueprint(connections_internal_bp)
    quart_app.config["dal"] = dal
    quart_app.config["async_dal"] = dal  # placeholder -- every connections_svc call is mocked
    quart_app.config["HUB_API_CONFIG"] = _test_hub_config()
    quart_app.config["SERVICE_JWT_ISSUER"] = _service_issuer()
    quart_app.config["SERVICE_JWT_VERIFIER"] = quart_app.config["SERVICE_JWT_ISSUER"].as_verifier()
    return quart_app


def _service_issuer() -> ServiceJwtIssuer:
    """Real Ed25519 issuer allow-listing one system-tenant service for ``connections:resolve``."""
    private = Ed25519PrivateKey.generate()
    identity = ServiceIdentity(
        service_id=SERVICE_ID,
        k8s_namespace="waddlebot",
        k8s_service_account="svc-action",
        allowed_scopes=frozenset({"connections:resolve"}),
        tenant="system",
    )
    return ServiceJwtIssuer(
        keys={"k1": SigningKey("k1", private, private.public_key())},
        active_kid="k1",
        identities={SERVICE_ID: identity},
    )


@pytest.fixture
def mint_headers(app: Quart) -> Any:
    """Factory: ``mint_headers(community_id)`` -> Bearer header of a community-bound machine JWT."""

    def _mint(community_id: int, *, scope: str = "connections:resolve") -> dict[str, str]:
        token, _ = app.config["SERVICE_JWT_ISSUER"].issue_with_claims(
            SERVICE_ID, scope, ttl_seconds=300, extra_claims={"community": community_id}
        )
        return {"Authorization": f"Bearer {token}"}

    return _mint


@pytest.fixture
def bound_headers(mint_headers: Any, community_db: Any) -> dict[str, str]:
    """Bearer header bound to the seeded test community."""
    return mint_headers(community_db[1])


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connections_module, "feature_enabled", AsyncMock(return_value=True))


def _insert_other_tenant(dal: Any, *, slug: str = "other-corp") -> None:
    dal.tenants.insert(slug=slug, is_active=True)
    dal.commit()


class TestListConnections:
    async def test_success_includes_callback_base(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        statuses = [
            ConnectionStatus(
                provider="youtube",
                connected=True,
                scopes=["a"],
                expires_at=None,
                updated_at=None,
                account_label="My Channel",
            ),
            ConnectionStatus(
                provider="spotify",
                connected=False,
                scopes=[],
                expires_at=None,
                updated_at=None,
                account_label=None,
            ),
        ]
        mock_list = AsyncMock(return_value=statuses)
        monkeypatch.setattr(connections_module.connections_svc, "list_connections", mock_list)

        response = await client.get(
            f"/api/v1/communities/{community_id}/connections",
            headers=user_auth_headers(user_id=1, scope="community.connections:read"),
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["callback_base"] == CALLBACK_BASE
        assert body["data"]["connections"][0]["provider"] == "youtube"
        assert body["data"]["connections"][0]["connected"] is True
        assert body["data"]["connections"][1]["connected"] is False
        mock_list.assert_awaited_once()

    async def test_feature_disabled_is_402(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(connections_module, "feature_enabled", AsyncMock(return_value=False))

        response = await client.get(
            f"/api/v1/communities/{community_id}/connections",
            headers=user_auth_headers(user_id=1, scope="community.connections:read"),
        )
        assert response.status_code == 402

    async def test_wrong_scope_is_403(
        self, client: Any, user_auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/communities/{community_id}/connections",
            headers=user_auth_headers(user_id=1, scope="something:else"),
        )
        assert response.status_code == 403

    async def test_cross_tenant_community_is_404(
        self, client: Any, user_auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        _insert_other_tenant(dal)
        response = await client.get(
            f"/api/v1/communities/{community_id}/connections",
            headers=user_auth_headers(
                user_id=1, scope="community.connections:read", tenant="other-corp"
            ),
        )
        assert response.status_code == 404


class TestAuthorizeConnection:
    async def test_success_without_pkce_builds_redirect_uri_from_config(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(connections_module.providers_svc, "get_provider", lambda _name: _spec())
        captured: dict[str, Any] = {}

        def _build_authorize_url(_name: str, **kwargs: Any) -> str:
            captured.update(kwargs)
            return "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"

        monkeypatch.setattr(
            connections_module.providers_svc, "build_authorize_url", _build_authorize_url
        )
        mock_create_state = AsyncMock(return_value="state-token-abc")
        monkeypatch.setattr(connections_module.state_svc, "create_state", mock_create_state)

        response = await client.post(
            f"/api/v1/communities/{community_id}/connections/youtube/authorize",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["authorize_url"].startswith("https://accounts.google.com")
        assert captured["redirect_uri"] == f"{CALLBACK_BASE}/api/v1/connections/callback/youtube"
        assert captured["state"] == "state-token-abc"
        assert captured["code_challenge"] is None
        create_kwargs = mock_create_state.await_args.kwargs
        assert create_kwargs["community_id"] == community_id
        assert create_kwargs["provider"] == "youtube"
        assert create_kwargs["code_verifier"] is None
        expected_redirect = f"{CALLBACK_BASE}/api/v1/connections/callback/youtube"
        assert create_kwargs["redirect_uri"] == expected_redirect

    async def test_success_with_pkce_generates_verifier_and_challenge(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(
            connections_module.providers_svc, "get_provider", lambda _name: _spec(uses_pkce=True)
        )
        monkeypatch.setattr(
            connections_module.providers_svc,
            "make_pkce_pair",
            lambda: ("verifier-value", "challenge-value"),
        )
        captured: dict[str, Any] = {}

        def _build_authorize_url(_name: str, **kwargs: Any) -> str:
            captured.update(kwargs)
            return "https://id.kick.com/oauth/authorize?client_id=x"

        monkeypatch.setattr(
            connections_module.providers_svc, "build_authorize_url", _build_authorize_url
        )
        mock_create_state = AsyncMock(return_value="state-token-xyz")
        monkeypatch.setattr(connections_module.state_svc, "create_state", mock_create_state)

        response = await client.post(
            f"/api/v1/communities/{community_id}/connections/kick/authorize",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )

        assert response.status_code == 200
        assert captured["code_challenge"] == "challenge-value"
        assert mock_create_state.await_args.kwargs["code_verifier"] == "verifier-value"

    async def test_unsupported_provider_is_400(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db

        def _get_provider(_name: str) -> ProviderSpec:
            raise ValueError("unsupported provider")

        monkeypatch.setattr(connections_module.providers_svc, "get_provider", _get_provider)

        response = await client.post(
            f"/api/v1/communities/{community_id}/connections/not-a-provider/authorize",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )
        assert response.status_code == 400

    async def test_provider_not_configured_is_503(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(connections_module.providers_svc, "get_provider", lambda _name: _spec())
        monkeypatch.setattr(
            connections_module.state_svc, "create_state", AsyncMock(return_value="s")
        )

        def _build_authorize_url(_name: str, **_kwargs: Any) -> str:
            raise ProviderNotConfigured("missing client id")

        monkeypatch.setattr(
            connections_module.providers_svc, "build_authorize_url", _build_authorize_url
        )

        response = await client.post(
            f"/api/v1/communities/{community_id}/connections/youtube/authorize",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )
        assert response.status_code == 503
        body = await response.get_json()
        assert body == {"error": "provider_not_configured", "provider": "youtube"}

    async def test_feature_disabled_is_402(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(connections_module, "feature_enabled", AsyncMock(return_value=False))

        response = await client.post(
            f"/api/v1/communities/{community_id}/connections/youtube/authorize",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )
        assert response.status_code == 402

    async def test_cross_tenant_community_is_404(
        self, client: Any, user_auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        _insert_other_tenant(dal)
        response = await client.post(
            f"/api/v1/communities/{community_id}/connections/youtube/authorize",
            headers=user_auth_headers(
                user_id=1, scope="community.connections:write", tenant="other-corp"
            ),
        )
        assert response.status_code == 404


class TestOAuthCallback:
    """PUBLIC route -- no Authorization header on any request in this class."""

    async def test_success_renders_html_with_no_auth_and_no_token_leak(
        self, client: Any, monkeypatch: Any
    ) -> None:
        payload = StatePayload(
            community_id=1,
            provider="youtube",
            user_id=1,
            redirect_uri=f"{CALLBACK_BASE}/api/v1/connections/callback/youtube",
            code_verifier=None,
        )
        monkeypatch.setattr(
            connections_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        monkeypatch.setattr(
            connections_module.providers_svc,
            "exchange_code",
            AsyncMock(
                return_value=TokenResponse(
                    access_token="SECRET_ACCESS_TOKEN_VALUE",  # noqa: S106
                    refresh_token="SECRET_REFRESH_TOKEN_VALUE",  # noqa: S106
                    expires_in=3600,
                    scopes=["a"],
                    token_type="Bearer",  # noqa: S106
                )
            ),
        )
        monkeypatch.setattr(
            connections_module.providers_svc,
            "fetch_account_label",
            AsyncMock(return_value="My Channel"),
        )
        mock_upsert = AsyncMock(
            return_value=ConnectionStatus(
                provider="youtube",
                connected=True,
                scopes=["a"],
                expires_at=None,
                updated_at=None,
                account_label="My Channel",
            )
        )
        monkeypatch.setattr(connections_module.connections_svc, "upsert_connection", mock_upsert)

        response = await client.get(
            "/api/v1/connections/callback/youtube?state=abc123&code=auth-code-xyz"
        )

        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/html")
        expected_csp = "default-src 'none'; script-src 'unsafe-inline'"
        assert response.headers["Content-Security-Policy"] == expected_csp
        body_text = await response.get_data(as_text=True)
        assert '"ok": true' in body_text or '"ok":true' in body_text
        assert "SECRET_ACCESS_TOKEN_VALUE" not in body_text
        assert "SECRET_REFRESH_TOKEN_VALUE" not in body_text
        assert "auth-code-xyz" not in body_text
        assert "abc123" not in body_text
        assert "You can close this window" in body_text
        upsert_kwargs = mock_upsert.await_args.kwargs
        assert upsert_kwargs["access_token"] == "SECRET_ACCESS_TOKEN_VALUE"
        assert upsert_kwargs["actor_user_id"] == 1

    async def test_invalid_or_expired_state_is_400(self, client: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(
            connections_module.state_svc, "consume_state", AsyncMock(return_value=None)
        )
        response = await client.get(
            "/api/v1/connections/callback/youtube?state=bogus&code=auth-code"
        )
        assert response.status_code == 400
        body_text = await response.get_data(as_text=True)
        assert "invalid_or_expired_state" in body_text
        assert "auth-code" not in body_text

    async def test_provider_error_param_skips_exchange(self, client: Any, monkeypatch: Any) -> None:
        payload = StatePayload(
            community_id=1,
            provider="youtube",
            user_id=1,
            redirect_uri="http://x/cb",
            code_verifier=None,
        )
        monkeypatch.setattr(
            connections_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        mock_exchange = AsyncMock()
        monkeypatch.setattr(connections_module.providers_svc, "exchange_code", mock_exchange)

        response = await client.get(
            "/api/v1/connections/callback/youtube?state=abc&error=access_denied"
        )

        body_text = await response.get_data(as_text=True)
        assert "access_denied" in body_text
        mock_exchange.assert_not_awaited()

    async def test_missing_code_is_400(self, client: Any, monkeypatch: Any) -> None:
        payload = StatePayload(
            community_id=1,
            provider="youtube",
            user_id=1,
            redirect_uri="http://x/cb",
            code_verifier=None,
        )
        monkeypatch.setattr(
            connections_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        response = await client.get("/api/v1/connections/callback/youtube?state=abc")
        assert response.status_code == 400
        body_text = await response.get_data(as_text=True)
        assert "missing_code" in body_text

    async def test_exchange_failure_renders_error_page(self, client: Any, monkeypatch: Any) -> None:
        payload = StatePayload(
            community_id=1,
            provider="youtube",
            user_id=1,
            redirect_uri="http://x/cb",
            code_verifier=None,
        )
        monkeypatch.setattr(
            connections_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        monkeypatch.setattr(
            connections_module.providers_svc,
            "exchange_code",
            AsyncMock(side_effect=OAuthExchangeError("youtube: token endpoint returned HTTP 400")),
        )

        response = await client.get("/api/v1/connections/callback/youtube?state=abc&code=auth-code")
        body_text = await response.get_data(as_text=True)
        assert "exchange_failed" in body_text
        assert "auth-code" not in body_text

    async def test_upsert_failure_renders_error_page(self, client: Any, monkeypatch: Any) -> None:
        payload = StatePayload(
            community_id=1,
            provider="youtube",
            user_id=1,
            redirect_uri="http://x/cb",
            code_verifier=None,
        )
        monkeypatch.setattr(
            connections_module.state_svc, "consume_state", AsyncMock(return_value=payload)
        )
        monkeypatch.setattr(
            connections_module.providers_svc,
            "exchange_code",
            AsyncMock(
                return_value=TokenResponse(
                    access_token="AT",  # noqa: S106
                    refresh_token=None,
                    expires_in=3600,
                    scopes=["a"],
                    token_type="Bearer",  # noqa: S106
                )
            ),
        )
        monkeypatch.setattr(
            connections_module.providers_svc, "fetch_account_label", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            connections_module.connections_svc,
            "upsert_connection",
            AsyncMock(side_effect=UnsupportedProvider("unsupported provider 'youtube'")),
        )

        response = await client.get("/api/v1/connections/callback/youtube?state=abc&code=auth-code")
        body_text = await response.get_data(as_text=True)
        assert "save_failed" in body_text


class TestDeleteConnection:
    async def test_success_is_204(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(
            connections_module.connections_svc, "delete_connection", AsyncMock(return_value=True)
        )
        response = await client.delete(
            f"/api/v1/communities/{community_id}/connections/youtube",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )
        assert response.status_code == 204

    async def test_nothing_active_is_404(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(
            connections_module.connections_svc, "delete_connection", AsyncMock(return_value=False)
        )
        response = await client.delete(
            f"/api/v1/communities/{community_id}/connections/youtube",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )
        assert response.status_code == 404

    async def test_unsupported_provider_is_400(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(
            connections_module.connections_svc,
            "delete_connection",
            AsyncMock(side_effect=UnsupportedProvider("unsupported provider 'nope'")),
        )
        response = await client.delete(
            f"/api/v1/communities/{community_id}/connections/nope",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )
        assert response.status_code == 400

    async def test_wrong_scope_is_403(
        self, client: Any, user_auth_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        response = await client.delete(
            f"/api/v1/communities/{community_id}/connections/youtube",
            headers=user_auth_headers(user_id=1, scope="community.connections:read"),
        )
        assert response.status_code == 403

    async def test_feature_disabled_is_402(
        self, client: Any, user_auth_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(connections_module, "feature_enabled", AsyncMock(return_value=False))
        response = await client.delete(
            f"/api/v1/communities/{community_id}/connections/youtube",
            headers=user_auth_headers(user_id=1, scope="community.connections:write"),
        )
        assert response.status_code == 402

    async def test_cross_tenant_community_is_404(
        self, client: Any, user_auth_headers: Any, community_db: Any
    ) -> None:
        dal, community_id = community_db
        _insert_other_tenant(dal)
        response = await client.delete(
            f"/api/v1/communities/{community_id}/connections/youtube",
            headers=user_auth_headers(
                user_id=1, scope="community.connections:write", tenant="other-corp"
            ),
        )
        assert response.status_code == 404


class TestInternalGetToken:
    async def test_fresh_token_returned_without_refresh(
        self, client: Any, bound_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        tokens = DecryptedTokens(
            access_token="AT_FRESH",  # noqa: S106
            refresh_token="RT_FRESH",  # noqa: S106
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["a"],
        )
        monkeypatch.setattr(
            connections_module.connections_svc,
            "get_decrypted_tokens",
            AsyncMock(return_value=tokens),
        )
        mock_refresh = AsyncMock()
        monkeypatch.setattr(connections_module.providers_svc, "refresh_access_token", mock_refresh)

        response = await client.get(
            f"/api/v1/internal/communities/{community_id}/connections/youtube/token",
            headers=bound_headers,
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["access_token"] == "AT_FRESH"
        mock_refresh.assert_not_awaited()

    async def test_near_expiry_token_is_refreshed_and_persisted(
        self, client: Any, bound_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        tokens = DecryptedTokens(
            access_token="AT_OLD",  # noqa: S106
            refresh_token="RT_OLD",  # noqa: S106
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
            scopes=["a"],
        )
        monkeypatch.setattr(
            connections_module.connections_svc,
            "get_decrypted_tokens",
            AsyncMock(return_value=tokens),
        )
        monkeypatch.setattr(
            connections_module.providers_svc,
            "refresh_access_token",
            AsyncMock(
                return_value=TokenResponse(
                    access_token="AT_NEW",  # noqa: S106
                    refresh_token=None,
                    expires_in=3600,
                    scopes=["a", "b"],
                    token_type="Bearer",  # noqa: S106
                )
            ),
        )
        mock_store = AsyncMock()
        monkeypatch.setattr(
            connections_module.connections_svc, "store_refreshed_access_token", mock_store
        )

        response = await client.get(
            f"/api/v1/internal/communities/{community_id}/connections/youtube/token",
            headers=bound_headers,
        )

        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["access_token"] == "AT_NEW"
        # Provider omitted refresh_token on this refresh -- falls back to the existing one.
        assert body["data"]["refresh_token"] == "RT_OLD"
        assert body["data"]["scopes"] == ["a", "b"]
        mock_store.assert_awaited_once()
        assert mock_store.await_args.kwargs["access_token"] == "AT_NEW"

    async def test_not_connected_is_404(
        self, client: Any, bound_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(
            connections_module.connections_svc,
            "get_decrypted_tokens",
            AsyncMock(return_value=None),
        )
        response = await client.get(
            f"/api/v1/internal/communities/{community_id}/connections/youtube/token",
            headers=bound_headers,
        )
        assert response.status_code == 404

    async def test_refresh_failure_is_502(
        self, client: Any, bound_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        tokens = DecryptedTokens(
            access_token="AT_OLD",  # noqa: S106
            refresh_token="RT_OLD",  # noqa: S106
            expires_at=datetime.now(UTC) - timedelta(seconds=5),
            scopes=["a"],
        )
        monkeypatch.setattr(
            connections_module.connections_svc,
            "get_decrypted_tokens",
            AsyncMock(return_value=tokens),
        )
        monkeypatch.setattr(
            connections_module.providers_svc,
            "refresh_access_token",
            AsyncMock(side_effect=OAuthExchangeError("youtube: token endpoint returned HTTP 400")),
        )

        response = await client.get(
            f"/api/v1/internal/communities/{community_id}/connections/youtube/token",
            headers=bound_headers,
        )
        assert response.status_code == 502
        body = await response.get_json()
        assert body == {"error": "refresh_failed"}

    async def test_missing_service_key_is_401(self, client: Any, community_db: Any) -> None:
        _, community_id = community_db
        response = await client.get(
            f"/api/v1/internal/communities/{community_id}/connections/youtube/token"
        )
        assert response.status_code == 401

    async def test_unknown_community_is_404(self, client: Any, mint_headers: Any) -> None:
        response = await client.get(
            "/api/v1/internal/communities/999999/connections/youtube/token",
            headers=mint_headers(999999),
        )
        assert response.status_code == 404

    async def test_unsupported_provider_is_400(
        self, client: Any, bound_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(
            connections_module.connections_svc,
            "get_decrypted_tokens",
            AsyncMock(side_effect=UnsupportedProvider("unsupported provider 'nope'")),
        )
        response = await client.get(
            f"/api/v1/internal/communities/{community_id}/connections/nope/token",
            headers=bound_headers,
        )
        assert response.status_code == 400


class TestInternalGetTokenAuth:
    """HIGH-2: the token route accepts ONLY a community-bound machine JWT, never a static key."""

    URL = "/api/v1/internal/communities/{cid}/connections/youtube/token"

    async def test_service_key_header_alone_is_401(
        self, client: Any, service_key_headers: Any, community_db: Any
    ) -> None:
        # regression: HIGH-2 -- a valid shared X-Service-Key with NO Bearer must not authenticate.
        _, community_id = community_db
        response = await client.get(self.URL.format(cid=community_id), headers=service_key_headers)
        assert response.status_code == 401

    async def test_valid_service_key_plus_bearer_for_other_community_is_403(
        self, client: Any, service_key_headers: Any, mint_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        headers = {**service_key_headers, **mint_headers(community_id + 1)}
        response = await client.get(self.URL.format(cid=community_id), headers=headers)
        assert response.status_code == 403

    async def test_unconfigured_verifier_is_503_not_open(
        self, client: Any, app: Quart, service_key_headers: Any, community_db: Any
    ) -> None:
        _, community_id = community_db
        app.config["SERVICE_JWT_VERIFIER"] = None
        response = await client.get(self.URL.format(cid=community_id), headers=service_key_headers)
        assert response.status_code == 503

    async def test_wrong_scope_is_401(
        self, client: Any, mint_headers: Any, community_db: Any, app: Quart
    ) -> None:
        _, community_id = community_db
        ident = app.config["SERVICE_JWT_ISSUER"].identities[SERVICE_ID]
        app.config["SERVICE_JWT_ISSUER"].identities[SERVICE_ID] = ServiceIdentity(
            ident.service_id,
            ident.k8s_namespace,
            ident.k8s_service_account,
            frozenset({"connections:resolve", "other:scope"}),
            "system",
        )
        response = await client.get(
            self.URL.format(cid=community_id),
            headers=mint_headers(community_id, scope="other:scope"),
        )
        assert response.status_code == 401

    async def test_token_for_matching_community_returns_tokens_and_refreshes(
        self, client: Any, bound_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        tokens = DecryptedTokens(
            access_token="AT_OLD",  # noqa: S106
            refresh_token="RT_OLD",  # noqa: S106
            expires_at=datetime.now(UTC) + timedelta(seconds=10),  # inside the 60s skew
            scopes=["a"],
        )
        monkeypatch.setattr(
            connections_module.connections_svc,
            "get_decrypted_tokens",
            AsyncMock(return_value=tokens),
        )
        monkeypatch.setattr(
            connections_module.providers_svc,
            "refresh_access_token",
            AsyncMock(
                return_value=TokenResponse(
                    access_token="AT_NEW",  # noqa: S106
                    refresh_token=None,
                    expires_in=3600,
                    scopes=["a"],
                    token_type="Bearer",  # noqa: S106
                )
            ),
        )
        monkeypatch.setattr(
            connections_module.connections_svc, "store_refreshed_access_token", AsyncMock()
        )
        response = await client.get(self.URL.format(cid=community_id), headers=bound_headers)
        assert response.status_code == 200
        assert (await response.get_json())["data"]["access_token"] == "AT_NEW"

    async def test_endpoint_response_is_no_store(
        self, client: Any, bound_headers: Any, community_db: Any, monkeypatch: Any
    ) -> None:
        _, community_id = community_db
        monkeypatch.setattr(
            connections_module.connections_svc,
            "get_decrypted_tokens",
            AsyncMock(
                return_value=DecryptedTokens(
                    access_token="AT",  # noqa: S106
                    refresh_token=None,
                    expires_at=None,
                    scopes=[],
                )
            ),
        )
        response = await client.get(self.URL.format(cid=community_id), headers=bound_headers)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        # 401s carry it too: nothing under /internal may be cached.
        denied = await client.get(self.URL.format(cid=community_id))
        assert denied.status_code == 401
        assert denied.headers["Cache-Control"] == "no-store"


def test_no_service_key_import_in_community_connections() -> None:
    """AST guard: the connection-token module never imports or calls the static-secret check."""
    path = Path(connections_module.__file__)
    assert path.is_file(), "scanned file must exist"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = list(ast.walk(tree))
    assert len(nodes) > 500, "scanned file unexpectedly small"
    imported = {a.name for n in nodes if isinstance(n, ast.ImportFrom) for a in n.names}
    used = {n.id for n in nodes if isinstance(n, ast.Name)}
    assert "is_valid_service_key" not in imported | used
