"""`blueprints/v1/platform_config.py` -- M3 superadmin platform-config group.

Ported from `platformConfigController.js`'s superadmin.js-mounted subset.
Same standalone-app / real-JWT / real-pydal pattern as
`test_v1_user_management_blueprint.py`, which this group shares its
`require_scope("users:admin")` gate with.

Fail-first proof (executed, not narrated): temporarily swapped
`require_scope("users:admin")` for `require_scope("platform:read")` on
`get_platform_configs`' decorator chain (matches the "wrong scope"
token `auth_headers(scope="platform:read")` already carries) --
`test_get_platform_configs_wrong_scope_is_403` went red (200 instead of
403); reverted, green again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.platform_config import platform_config_bp
from config import HubAPIConfig
from tests.conftest import TENANT_SLUG, make_user_token


def _test_config() -> HubAPIConfig:
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
        default_tenant_slug=TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
    )


@pytest.fixture
def app(platform_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(platform_config_bp)
    quart_app.config["dal"] = platform_db.dal
    quart_app.config["async_dal"] = platform_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _admin_headers(*, user_id: int = 1) -> dict[str, str]:
    token = make_user_token(
        user_id=user_id,
        scope="*:read *:write *:admin *:delete settings:write users:admin",
        tenant=TENANT_SLUG,
    )
    return {"Authorization": f"Bearer {token}"}


def _seed_bot_credential(
    platform_db: Any, *, platform: str = "twitch", access_token: str | None = "tok_abc"
) -> int:
    row_id: int = platform_db.dal.platform_integrations.insert(
        platform=platform,
        integration_type="bot",
        access_token=access_token,
        client_id="client-123",
        token_type="Bearer",
        is_active=True,
        is_encrypted=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    platform_db.dal.commit()
    return row_id


class TestScopeEnforcement:
    async def test_get_platform_configs_no_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/platform-config")
        assert response.status_code == 401

    async def test_get_platform_configs_wrong_scope_is_403(
        self, client: Any, auth_headers: Any
    ) -> None:
        response = await client.get(
            "/api/v1/superadmin/platform-config", headers=auth_headers(scope="platform:read")
        )
        assert response.status_code == 403

    async def test_get_platform_configs_with_scope_returns_200(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/platform-config", headers=_admin_headers())
        assert response.status_code == 200


class TestGetPlatformConfigs:
    async def test_masks_tokens(self, client: Any, platform_db: Any) -> None:
        _seed_bot_credential(platform_db)
        response = await client.get("/api/v1/superadmin/platform-config", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["count"] == 1
        cred = body["data"][0]
        assert cred["accessToken"] == "***"
        assert cred["clientId"] == "client-123"  # never masked

    async def test_filters_by_platform(self, client: Any, platform_db: Any) -> None:
        _seed_bot_credential(platform_db, platform="twitch")
        _seed_bot_credential(platform_db, platform="discord")
        response = await client.get(
            "/api/v1/superadmin/platform-config",
            headers=_admin_headers(),
            query_string={"platform": "discord"},
        )
        body = await response.get_json()
        assert body["count"] == 1
        assert body["data"][0]["platform"] == "discord"

    async def test_filters_by_integration_type(self, client: Any, platform_db: Any) -> None:
        _seed_bot_credential(platform_db, platform="twitch")
        response = await client.get(
            "/api/v1/superadmin/platform-config",
            headers=_admin_headers(),
            query_string={"integrationType": "user_oauth"},
        )
        body = await response.get_json()
        assert body["count"] == 0


class TestUpdatePlatformConfig:
    async def test_always_404s(self, client: Any, platform_db: Any) -> None:
        """See services/platform_config_service.py's docstring: a pre-existing Node bug."""
        _seed_bot_credential(platform_db, platform="twitch")
        response = await client.put(
            "/api/v1/superadmin/platform-config/twitch",
            headers=_admin_headers(),
            json={"clientId": "new-id"},
        )
        assert response.status_code == 404


class TestCrossTenantMetadataIsSuperadminOnly:
    """# regression: gh-969 -- `pi_list_metadata` is cross-tenant BY DESIGN (all tenants' rows).

    The only caller is `GET /superadmin/platform-config`, gated by `users:admin`: present ONLY in
    `SCOPE_BUNDLES["global"]["admin"]` (granted iff `hub_users.is_super_admin`) and NEVER in the
    tenant bundle -- so no tenant admin/owner/maintainer can reach it.
    """

    async def test_no_tenant_scope_bundle_can_list_platform_config(self, client: Any) -> None:
        from flask_core.auth import SCOPE_BUNDLES

        examined = 0
        for level in ("admin", "maintainer", "viewer"):
            scope = " ".join(SCOPE_BUNDLES["tenant"][level])
            token = make_user_token(user_id=1, scope=scope, tenant=TENANT_SLUG)
            r = await client.get(
                "/api/v1/superadmin/platform-config",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 403, (level, r.status_code)
            examined += 1
        assert examined == 3
        assert "users:admin" not in {s for v in SCOPE_BUNDLES["tenant"].values() for s in v}
        assert "users:admin" in SCOPE_BUNDLES["global"]["admin"]

    async def test_only_users_admin_scope_passes(self, client: Any) -> None:
        token = make_user_token(user_id=1, scope="users:admin", tenant=TENANT_SLUG)
        r = await client.get(
            "/api/v1/superadmin/platform-config", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200


class TestListFilters:
    """# regression: gh-969 -- filters on the metadata read (type/platform/invalid/empty)."""

    async def test_filters_and_invalid_type(self, client: Any, platform_db: Any) -> None:
        _seed_bot_credential(platform_db, platform="twitch")
        for query, expected in (
            ("", 1),
            ("?integrationType=bot", 1),
            ("?platform=twitch", 1),
            ("?platform=discord", 0),
            ("?integrationType=community_oauth", 0),
        ):
            r = await client.get(
                f"/api/v1/superadmin/platform-config{query}", headers=_admin_headers()
            )
            assert r.status_code == 200 and (await r.get_json())["count"] == expected, query
        bad = await client.get(
            "/api/v1/superadmin/platform-config?integrationType=nope", headers=_admin_headers()
        )
        assert bad.status_code == 400


class TestTestPlatformConnectionDelegatesToCredentialManager:
    """# regression: gh-969 -- hub-api cannot read operator bot tokens (H-3).

    `POST /platform-config/<platform>/test` is delegated to the credential manager (the
    sanctioned operator-secret holder). hub-api sends the platform + the caller's bearer only,
    never touches the bot token, and fails LOUD (503) when the credential manager is down.
    """

    URL_PREFIX = "https://credential-manager.waddlebot.svc:8095/api/v1/credentials/"

    def _patch_cm(self, monkeypatch: Any, handler: Any) -> list[httpx.Request]:
        monkeypatch.setenv(
            "CREDENTIAL_MANAGER_URL", "https://credential-manager.waddlebot.svc:8095"
        )
        seen: list[httpx.Request] = []
        real_async_client = httpx.AsyncClient

        def _recording(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return handler(req)

        def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(_recording)
            return real_async_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)
        return seen

    async def _post(
        self, client: Any, platform: str = "twitch", headers: dict[str, str] | None = None
    ) -> Any:
        return await client.post(
            f"/api/v1/superadmin/platform-config/{platform}/test",
            headers=headers or _admin_headers(),
        )

    async def test_valid_verdict_forwards_caller_bearer_and_never_the_token(
        self, client: Any, platform_db: Any, monkeypatch: Any
    ) -> None:
        _seed_bot_credential(platform_db, platform="twitch", access_token="CANARY-BOT-TOKEN")
        seen = self._patch_cm(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"platform": "twitch", "valid": True, "error": None},
                },
            ),
        )
        headers = _admin_headers()
        response = await self._post(client, headers=headers)
        body = await response.get_json()
        assert response.status_code == 200 and body["data"]["valid"] is True
        assert len(seen) == 1
        assert str(seen[0].url) == f"{self.URL_PREFIX}twitch/test" and seen[0].method == "POST"
        assert seen[0].headers["authorization"] == headers["Authorization"]
        assert b"CANARY-BOT-TOKEN" not in seen[0].content and "CANARY-BOT-TOKEN" not in str(
            seen[0].url
        )

    async def test_invalid_verdict_is_relayed(self, client: Any, monkeypatch: Any) -> None:
        self._patch_cm(
            monkeypatch,
            lambda r: httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"platform": "discord", "valid": False, "error": "HTTP 401"},
                },
            ),
        )
        body = await (await self._post(client, "discord")).get_json()
        assert body["data"]["valid"] is False and body["data"]["error"] == "HTTP 401"

    async def test_missing_credential_is_404(self, client: Any, monkeypatch: Any) -> None:
        self._patch_cm(monkeypatch, lambda r: httpx.Response(404, json={"success": False}))
        assert (await self._post(client)).status_code == 404

    @pytest.mark.parametrize("status", [401, 403, 500, 502])
    async def test_credential_manager_errors_are_a_loud_503(
        self, client: Any, monkeypatch: Any, status: int
    ) -> None:
        self._patch_cm(monkeypatch, lambda r: httpx.Response(status, json={}))
        response = await self._post(client)
        body = await response.get_json()
        assert response.status_code == 503
        assert body["error"]["code"] == "CREDENTIAL_MANAGER_UNAVAILABLE"

    async def test_unreachable_credential_manager_is_a_loud_503_not_a_pass(
        self, client: Any, monkeypatch: Any
    ) -> None:
        def _raise(_req: Any) -> httpx.Response:
            raise httpx.ConnectError("boom")

        self._patch_cm(monkeypatch, _raise)
        response = await self._post(client)
        assert response.status_code == 503
        assert "valid" not in str(await response.get_json())

    async def test_malformed_response_is_a_loud_503(self, client: Any, monkeypatch: Any) -> None:
        self._patch_cm(monkeypatch, lambda r: httpx.Response(200, json={"nope": 1}))
        assert (await self._post(client)).status_code == 503

    async def test_invalid_platform_is_400_and_never_calls_out(
        self, client: Any, monkeypatch: Any
    ) -> None:
        seen = self._patch_cm(monkeypatch, lambda r: httpx.Response(200, json={}))
        assert (await self._post(client, "..%2Fx")).status_code in (400, 404)
        assert (await self._post(client, "UPPER")).status_code == 400
        assert seen == []


class TestHubSettings:
    async def test_get_settings_empty(self, client: Any) -> None:
        response = await client.get("/api/v1/superadmin/settings", headers=_admin_headers())
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"] == {}

    async def test_update_settings_upserts(self, client: Any, platform_db: Any) -> None:
        response = await client.put(
            "/api/v1/superadmin/settings",
            headers=_admin_headers(),
            json={"signup_enabled": "true"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["signup_enabled"] == "true"

        # Update again -- must UPDATE the existing row, not duplicate it.
        response2 = await client.put(
            "/api/v1/superadmin/settings",
            headers=_admin_headers(),
            json={"signup_enabled": "false"},
        )
        body2 = await response2.get_json()
        assert body2["data"]["signup_enabled"] == "false"
        rows = await platform_db.select_async(
            platform_db.dal(platform_db.dal.hub_settings.setting_key == "signup_enabled")
        )
        assert len(rows) == 1

    async def test_update_settings_non_dict_body_is_400(self, client: Any) -> None:
        response = await client.put(
            "/api/v1/superadmin/settings", headers=_admin_headers(), json=[1, 2, 3]
        )
        assert response.status_code == 400
