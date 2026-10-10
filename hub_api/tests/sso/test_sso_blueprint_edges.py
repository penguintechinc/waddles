"""Blueprint-level edge cases: schema skew, settings bootstrap, malformed requests."""

from __future__ import annotations

import json
import types
from typing import Any

import pytest

import blueprints.v1.sso as sso_bp
from services.sso_settings import SsoSettings
from tests.sso.kit import Kit

ADMIN_URL = "/api/v1/tenant/sso/connections"


@pytest.fixture
def no_schema(kit: Kit) -> Kit:
    """hub-api running ahead of migration 0049: no SSO tables are reflected."""
    kit.app.config["install_dal"] = types.SimpleNamespace()
    return kit


class TestSchemaSkewIsALoud503EverywhereNeverA500:
    async def test_admin_routes(self, no_schema: Kit) -> None:
        kit = no_schema
        pid = "00000000-0000-0000-0000-000000000000"
        responses = [
            await kit.client.get(ADMIN_URL, headers=kit.headers()),
            await kit.post_connection(kit.oidc_body()),
            await kit.get_connection(pid),
            await kit.patch_connection(pid, {"enabled": False}),
            await kit.delete_connection(pid),
        ]
        assert [r.status_code for r in responses] == [503] * 5
        for r in responses:
            body = await r.get_json()
            assert body["error"]["code"] == "SSO_UNAVAILABLE"
            assert "0049_sso_connections" in body["error"]["message"]

    async def test_public_routes(self, no_schema: Kit) -> None:
        kit = no_schema
        options = await kit.client.get("/api/v1/auth/sso/options?tenant=acme-corp")
        assert options.status_code == 503
        metadata = await kit.client.get("/api/v1/auth/sso/x/metadata")
        assert metadata.status_code == 503
        start = await kit.client.get("/api/v1/auth/sso/x/start")
        assert start.status_code == 503
        login = await kit.client.get("/api/v1/auth/sso/x/login")
        assert kit.error_reason(login) == "sso_unavailable"


class TestWiring:
    async def test_missing_tenant_context_is_a_403(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sso_bp, "get_tenant_context", lambda _request: None)
        response = await kit.client.get(ADMIN_URL, headers=kit.headers())
        assert response.status_code == 403

    async def test_settings_are_loaded_from_the_environment_once_and_cached(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        del kit.app.config[sso_bp.SETTINGS_CONFIG_KEY]
        monkeypatch.setenv("SSO_STATE_TTL_SECONDS", "321")
        await kit.client.get(ADMIN_URL, headers=kit.headers())
        cached = kit.app.config[sso_bp.SETTINGS_CONFIG_KEY]
        assert isinstance(cached, SsoSettings)
        assert cached.state_ttl_s == 321
        monkeypatch.setenv("SSO_STATE_TTL_SECONDS", "999")
        await kit.client.get(ADMIN_URL, headers=kit.headers())
        assert kit.app.config[sso_bp.SETTINGS_CONFIG_KEY].state_ttl_s == 321

    async def test_malformed_operator_settings_fail_loudly_not_silently(
        self, kit: Kit, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        del kit.app.config[sso_bp.SETTINGS_CONFIG_KEY]
        monkeypatch.setenv("SSO_CLOCK_SKEW_SECONDS", "banana")
        with caplog.at_level("ERROR"):
            admin = await kit.client.get(ADMIN_URL, headers=kit.headers())
            callback = await kit.client.get("/api/v1/auth/sso/x/callback?code=c&state=s")
            acs = await kit.client.post(
                "/api/v1/auth/sso/x/acs", form={"SAMLResponse": "a", "RelayState": "b"}
            )
        assert admin.status_code == 503
        assert "SSO_CLOCK_SKEW_SECONDS" in (await admin.get_json())["error"]["message"]
        assert kit.error_reason(callback) == "sso_unavailable"
        assert kit.error_reason(acs) == "sso_unavailable"
        assert "err_code=bad_setting" in caplog.text


class TestRequestShapes:
    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            "[]",
            json.dumps({"protocol": "oidc", "displayName": "x", "allowedDomains": "acme.test"}),
            json.dumps(
                {
                    "protocol": "oidc",
                    "displayName": "x",
                    "allowedDomains": ["a.test"],
                    "scopes": "openid",
                }
            ),
            json.dumps({"protocol": "oidc", "displayName": 5, "allowedDomains": ["a.test"]}),
            json.dumps({"protocol": "oidc", "displayName": "x", "allowedDomains": [1, 2]}),
        ],
    )
    async def test_malformed_bodies_are_400_not_500(self, kit: Kit, body: str) -> None:
        response = await kit.client.post(
            ADMIN_URL,
            headers={**kit.headers(), "Content-Type": "application/json"},
            data=body,
        )
        assert response.status_code == 400

    async def test_patch_rejects_unknown_fields(self, kit: Kit) -> None:
        public_id = await kit.create(kit.oidc_body())
        response = await kit.patch_connection(public_id, {"tenantId": 99})
        assert response.status_code == 400

    async def test_tenant_cannot_be_chosen_by_the_client(self, kit: Kit) -> None:
        body: dict[str, Any] = kit.oidc_body()
        body["tenant"] = "other-corp"
        assert (await kit.post_connection(body)).status_code == 400

    async def test_options_tenant_param_is_length_bounded(self, kit: Kit) -> None:
        response = await kit.client.get("/api/v1/auth/sso/options?tenant=" + "a" * 5000)
        assert response.status_code == 200
        assert (await response.get_json())["options"] == []
