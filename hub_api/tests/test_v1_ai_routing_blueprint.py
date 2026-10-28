"""`blueprints/v1/ai_routing.py` -- standalone Quart app, `ai_routing_db` fixture, real JWTs.

Mirrors `test_v1_overlay_blueprint.py`'s own pattern (see that file's
docstring): only `ai_config_bp`/`ai_completions_bp` registered, real pydal
queries via `services.community_access`, no mocking of the auth chain.
`route_completion()` itself is monkeypatched at the blueprint layer -- its
own real-dispatch behavior has full coverage in `test_ai_routing_router.py`;
this file only needs to prove the route wiring (auth, DTO validation,
response shape) is correct.

Fail-first proof (executed, not narrated): temporarily removed the
`await _require_admin(community_id)` call from `set_byok_key`'s handler
(leaving only `tenant_middleware`) -- `test_set_byok_key_non_admin_member_is_403`
went red (200 instead of 403: any active member, not just an admin, could
set/rotate another admin's BYOK key); reverted, confirmed green again.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core.ai_disclosure import with_ai_disclosure
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.ai_routing import ai_completions_bp, ai_config_bp
from config import HubAPIConfig
from services.ai_routing.models import AIResponse
from tests.conftest import (
    OTHER_TENANT_SLUG,
    TENANT_SLUG,
    make_user_token,
    seed_community,
    seed_membership,
)


def _test_config(*, ai_enabled: bool = True) -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8305,
        grpc_port=50305,
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
        identity_callback_base_url="http://localhost:8305",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
        overlay_base_url="https://overlay.example.test",
        ai_enabled=ai_enabled,
    )


@pytest.fixture
def app(ai_routing_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(ai_config_bp)
    quart_app.register_blueprint(ai_completions_bp)
    quart_app.config["dal"] = ai_routing_db.dal
    quart_app.config["async_dal"] = ai_routing_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture
def disabled_app(ai_routing_db: Any) -> Quart:
    """Same wiring as `app`, but `WADDLES_AI_ENABLED=false` -- `HubAPIConfig.ai_enabled=False`."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(ai_config_bp)
    quart_app.register_blueprint(ai_completions_bp)
    quart_app.config["dal"] = ai_routing_db.dal
    quart_app.config["async_dal"] = ai_routing_db
    quart_app.config["HUB_API_CONFIG"] = _test_config(ai_enabled=False)
    return quart_app


@pytest.fixture
def disabled_client(disabled_app: Quart) -> Any:
    return disabled_app.test_client()


def _admin_headers(
    ai_routing_db: Any, *, tenant: str = TENANT_SLUG
) -> tuple[dict[str, str], int, int]:
    community_id = seed_community(ai_routing_db, tenant_slug=tenant)
    user_id = 701
    seed_membership(
        ai_routing_db, community_id=community_id, user_id=user_id, role="community-owner"
    )
    token = make_user_token(user_id=user_id, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}, community_id, user_id


def _member_headers(
    ai_routing_db: Any, community_id: int, *, tenant: str = TENANT_SLUG, user_id: int = 702
) -> dict[str, str]:
    seed_membership(ai_routing_db, community_id=community_id, user_id=user_id, role="member")
    token = make_user_token(user_id=user_id, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}


class TestGetConfig:
    async def test_without_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/admin/1/ai/config")
        assert response.status_code == 401

    async def test_non_member_is_403(self, client: Any, ai_routing_db: Any) -> None:
        community_id = seed_community(ai_routing_db)
        token = make_user_token(user_id=999)  # never seeded as a member
        response = await client.get(
            f"/api/v1/admin/{community_id}/ai/config",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_admin_gets_default_free_config(self, client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await client.get(f"/api/v1/admin/{community_id}/ai/config", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["preferred_tier"] == "free"


class TestSetConfig:
    async def test_admin_can_update(self, client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/ai/config",
            headers=headers,
            json={"preferred_tier": "premium", "on_insufficient_balance": "block"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["preferred_tier"] == "premium"
        assert body["on_insufficient_balance"] == "block"

    async def test_member_cannot_update(self, client: Any, ai_routing_db: Any) -> None:
        _, community_id, _ = _admin_headers(ai_routing_db)
        member_headers = _member_headers(ai_routing_db, community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/ai/config",
            headers=member_headers,
            json={"preferred_tier": "premium"},
        )
        assert response.status_code == 403


class TestByokKeyEndpoints:
    async def test_set_byok_key_non_admin_member_is_403(
        self, client: Any, ai_routing_db: Any
    ) -> None:
        _, community_id, _ = _admin_headers(ai_routing_db)
        member_headers = _member_headers(ai_routing_db, community_id)
        response = await client.put(
            f"/api/v1/admin/{community_id}/ai/byok-keys",
            headers=member_headers,
            json={"provider": "openai", "api_key": "sk-whatever"},
        )
        assert response.status_code == 403

    async def test_set_and_list_byok_key_never_leaks_the_raw_key(
        self, client: Any, ai_routing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.ai_routing import config_service

        monkeypatch.setenv("AI_BYOK_ENCRYPTION_KEY", "33" * 32)

        async def _ok(provider: str, api_key: str, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(config_service, "validate_byok_key", _ok)

        headers, community_id, _ = _admin_headers(ai_routing_db)
        set_response = await client.put(
            f"/api/v1/admin/{community_id}/ai/byok-keys",
            headers=headers,
            json={"provider": "openai", "api_key": "sk-super-secret-do-not-leak"},
        )
        assert set_response.status_code == 200
        set_body = await set_response.get_json()
        assert set_body["key_last4"] == "leak"
        assert "sk-super-secret-do-not-leak" not in str(set_body)

        list_response = await client.get(
            f"/api/v1/admin/{community_id}/ai/byok-keys", headers=headers
        )
        list_body = await list_response.get_json()
        assert "sk-super-secret-do-not-leak" not in str(list_body)
        assert list_body["keys"][0]["provider"] == "openai"

    async def test_delete_byok_key_deactivates(
        self, client: Any, ai_routing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.ai_routing import config_service

        monkeypatch.setenv("AI_BYOK_ENCRYPTION_KEY", "44" * 32)

        async def _ok(provider: str, api_key: str, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(config_service, "validate_byok_key", _ok)

        headers, community_id, _ = _admin_headers(ai_routing_db)
        await client.put(
            f"/api/v1/admin/{community_id}/ai/byok-keys",
            headers=headers,
            json={"provider": "anthropic", "api_key": "sk-ant-value"},
        )
        response = await client.delete(
            f"/api/v1/admin/{community_id}/ai/byok-keys/anthropic", headers=headers
        )
        assert response.status_code == 200


class TestCompletions:
    async def test_without_token_is_401(self, client: Any) -> None:
        response = await client.post("/api/v1/community/1/ai/completions", json={"prompt": "hi"})
        assert response.status_code == 401

    async def test_non_member_is_403(self, client: Any, ai_routing_db: Any) -> None:
        community_id = seed_community(ai_routing_db)
        token = make_user_token(user_id=888)
        response = await client.post(
            f"/api/v1/community/{community_id}/ai/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"prompt": "hi"},
        )
        assert response.status_code == 403

    async def test_member_gets_a_completion(
        self, client: Any, ai_routing_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from blueprints.v1 import ai_routing as bp_module

        async def _fake_route_completion(*args: Any, **kwargs: Any) -> AIResponse:
            return AIResponse(
                text="hello from the model",
                provider="ollama",
                model="llama3.1:1b",
                tier_used="free",
                input_tokens=10,
                output_tokens=5,
            )

        monkeypatch.setattr(bp_module, "route_completion", _fake_route_completion)

        admin_headers, community_id, _ = _admin_headers(ai_routing_db)
        member_headers = _member_headers(ai_routing_db, community_id, user_id=703)
        response = await client.post(
            f"/api/v1/community/{community_id}/ai/completions",
            headers=member_headers,
            json={"prompt": "hi there"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["text"] == with_ai_disclosure("hello from the model")
        assert body["text"].endswith("AI-generated")
        assert body["tier_used"] == "free"
        assert body["input_tokens"] == 10

    async def test_invalid_requested_tier_is_400(self, client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await client.post(
            f"/api/v1/community/{community_id}/ai/completions",
            headers=headers,
            json={"prompt": "hi", "requested_tier": "super-deluxe"},
        )
        assert response.status_code == 400

    async def test_cross_tenant_community_is_403(self, client: Any, ai_routing_db: Any) -> None:
        """Same tenant-isolation guard `services.community_access` provides everywhere else."""
        other_community_id = seed_community(
            ai_routing_db, tenant_slug=OTHER_TENANT_SLUG, name="other"
        )
        seed_membership(ai_routing_db, community_id=other_community_id, user_id=704)
        # Token minted for TENANT_SLUG, but the community belongs to OTHER_TENANT_SLUG.
        token = make_user_token(user_id=704, tenant=TENANT_SLUG)
        response = await client.post(
            f"/api/v1/community/{other_community_id}/ai/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"prompt": "hi"},
        )
        assert response.status_code == 403


class TestAiDisabledKillSwitch:
    """`WADDLES_AI_ENABLED=false` (`HubAPIConfig.ai_enabled=False`) -- every route 503s.

    Uses `disabled_client`/`disabled_app` (`ai_enabled=False`), never the
    default `client`/`app` fixture. `route_completion()` and
    `config_service.*` are monkeypatched to raise `AssertionError` if
    called at all -- proof the `_ai_enabled()` guard is a real early-return
    in each handler, not just a status-code coincidence: reaching any of
    them here would fail the test even if the eventual response still
    happened to be a 503 for some other reason.
    """

    @pytest.fixture(autouse=True)
    def _fail_if_any_ai_backend_touched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from blueprints.v1 import ai_routing as bp_module
        from services.ai_routing import config_service

        async def _fail_route_completion(*args: Any, **kwargs: Any) -> AIResponse:
            raise AssertionError("route_completion() must never be called when AI is disabled")

        async def _fail_config_service(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("config_service.* must never be called when AI is disabled")

        monkeypatch.setattr(bp_module, "route_completion", _fail_route_completion)
        for fn_name in (
            "get_ai_config",
            "set_ai_config",
            "list_byok_keys",
            "set_byok_key",
            "delete_byok_key",
        ):
            monkeypatch.setattr(config_service, fn_name, _fail_config_service)

    async def test_get_config_is_503(self, disabled_client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await disabled_client.get(
            f"/api/v1/admin/{community_id}/ai/config", headers=headers
        )
        assert response.status_code == 503
        body = await response.get_json()
        assert body["error"]["code"] == "AI_DISABLED_DEPLOYMENT"

    async def test_set_config_is_503(self, disabled_client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await disabled_client.put(
            f"/api/v1/admin/{community_id}/ai/config",
            headers=headers,
            json={"preferred_tier": "premium"},
        )
        assert response.status_code == 503

    async def test_list_byok_keys_is_503(self, disabled_client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await disabled_client.get(
            f"/api/v1/admin/{community_id}/ai/byok-keys", headers=headers
        )
        assert response.status_code == 503

    async def test_set_byok_key_is_503(self, disabled_client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await disabled_client.put(
            f"/api/v1/admin/{community_id}/ai/byok-keys",
            headers=headers,
            json={"provider": "openai", "api_key": "sk-whatever"},
        )
        assert response.status_code == 503

    async def test_delete_byok_key_is_503(self, disabled_client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await disabled_client.delete(
            f"/api/v1/admin/{community_id}/ai/byok-keys/openai", headers=headers
        )
        assert response.status_code == 503

    async def test_create_completion_is_503(self, disabled_client: Any, ai_routing_db: Any) -> None:
        headers, community_id, _ = _admin_headers(ai_routing_db)
        response = await disabled_client.post(
            f"/api/v1/community/{community_id}/ai/completions",
            headers=headers,
            json={"prompt": "hi"},
        )
        assert response.status_code == 503
        body = await response.get_json()
        assert body["error"]["code"] == "AI_DISABLED_DEPLOYMENT"


async def test_enabled_app_is_unaffected(client: Any, ai_routing_db: Any) -> None:
    """Default `client` fixture (`ai_enabled=True`, unset default) -- unaffected, real logic runs.

    Module-level (not inside `TestAiDisabledKillSwitch`) so it does NOT
    inherit that class's autouse fail-if-touched fixture -- this test's
    whole point is that `config_service.get_ai_config` DOES get called.
    """
    headers, community_id, _ = _admin_headers(ai_routing_db)
    response = await client.get(f"/api/v1/admin/{community_id}/ai/config", headers=headers)
    assert response.status_code == 200
