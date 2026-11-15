"""`blueprints/v1/identity.py` -- the M1 self-service identity-linking group."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.identity import identity_bp
from config import HubAPIConfig
from tests.conftest import TENANT_SLUG, make_user_token


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="t",
        module_version="0",
        module_port=1,
        grpc_port=1,
        database_url="x",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="default",
        posthog_api_key=None,
        posthog_host="x",
        license_server_url="x",
        identity_callback_base_url="http://localhost",
        frontend_origin="http://localhost",
        log_level="INFO",
    )


@pytest.fixture
def app(auth_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(identity_bp)
    quart_app.config["dal"] = auth_db.dal
    quart_app.config["async_dal"] = auth_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _seed_user_with_identity(auth_db: Any) -> int:
    user_id: int = auth_db.dal.hub_users.insert(
        email="alice@example.com", username="alice", is_active=True, created_at=datetime.now(UTC)
    )
    auth_db.dal.hub_user_identities.insert(
        hub_user_id=user_id,
        platform="discord",
        platform_user_id="123",
        platform_username="alice#0001",
        is_primary=True,
        linked_at=datetime.now(UTC),
    )
    auth_db.dal.commit()
    return user_id


def _headers(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=user_id, tenant=TENANT_SLUG)}"}


class TestListIdentities:
    async def test_list_identities_without_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/user/identities")
        assert response.status_code == 401

    async def test_list_identities_returns_linked(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user_with_identity(auth_db)
        response = await client.get("/api/v1/user/identities", headers=_headers(user_id))
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["identities"]) == 1
        assert body["identities"][0]["platform"] == "discord"


class TestPrimaryIdentity:
    async def test_get_primary_identity(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user_with_identity(auth_db)
        response = await client.get("/api/v1/user/identities/primary", headers=_headers(user_id))
        assert response.status_code == 200
        body = await response.get_json()
        assert body["identity"]["platform"] == "discord"

    async def test_set_primary_identity_not_found_is_404(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user_with_identity(auth_db)
        response = await client.put(
            "/api/v1/user/identities/primary",
            headers=_headers(user_id),
            json={"platform": "twitch"},
        )
        assert response.status_code == 404


class TestUnlinkIdentity:
    async def test_unlink_last_identity_is_400(self, client: Any, auth_db: Any) -> None:
        user_id = _seed_user_with_identity(auth_db)
        response = await client.delete("/api/v1/user/identities/discord", headers=_headers(user_id))
        assert response.status_code == 400

    async def test_unlink_without_token_is_401(self, client: Any) -> None:
        response = await client.delete("/api/v1/user/identities/discord")
        assert response.status_code == 401
