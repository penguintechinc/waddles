"""`blueprints/v1/community_loyalty.py` -- MVP core-currency loyalty routes (gh-317).

Standalone Quart app registering both `loyalty_bp` (admin, JWT) and
`loyalty_internal_bp` (service-to-service, `X-Service-Key`) against this
file's own `loyalty_db` fixture -- same duplication rationale as `tests/
test_services_community_loyalty.py`'s own docstring: `tests/conftest.py`
is not in this PR's edit scope, so this feature gets its own small fixture
rather than a `conftest.py` edit, mirroring `music_station_db`'s shape.

Real JWTs via `tests.conftest.make_token`/`make_user_token`, real pydal
queries -- no mocking of the authz chain (`flask_core.tenancy.
tenant_middleware` / `flask_core.authz.require_scope`), same pattern as
`tests/test_v1_community_music_queue_blueprint.py`.
"""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.community_loyalty as loyalty_module
from blueprints.v1.community_loyalty import loyalty_bp, loyalty_internal_bp
from services.schema import bind_auth_tables, bind_loyalty_tables
from tests.community_ids import UNKNOWN_PUBLIC_ID, cpath, install_community_ids
from tests.conftest import TENANT_SLUG

SERVICE_API_KEY = "test-service-key"
U1_UUID = "11111111-1111-4111-8111-111111111111"
FOREIGN_UUID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def loyalty_db(tmp_path: Any) -> Any:
    """`(async_dal, community_id)` -- file-backed `AsyncDAL` with loyalty + auth tables bound."""
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'loyalty_bp_test.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_loyalty_tables(dal, migrate=True)
    tenant_id = dal.tenants.insert(slug=TENANT_SLUG, display_name="Acme Corp", is_active=True)
    dal.commit()
    community_id = dal.communities.insert(
        name="test-community", tenant_id=tenant_id, is_active=True
    )
    dal.commit()
    for table_name in dal.tables:
        dal(dal[table_name]).count()
    yield async_dal, community_id
    dal.close()


@pytest.fixture(autouse=True)
def _members(loyalty_db: Any) -> None:
    """`u1` (twitch) is a member of the fixture community; FOREIGN_UUID belongs to another one."""
    async_dal, community_id = loyalty_db
    dal = async_dal.dal
    other = dal.communities.insert(name="other-community", tenant_id=1, is_active=True)
    dal.community_members.insert(
        community_id=community_id, platform="twitch", platform_user_id="u1", user_uuid=U1_UUID
    )
    dal.community_members.insert(
        community_id=other, platform="twitch", platform_user_id="u9", user_uuid=FOREIGN_UUID
    )
    dal.commit()


@pytest.fixture
def app(loyalty_db: Any) -> Quart:
    async_dal, _community_id = loyalty_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    install_community_ids(quart_app, async_dal.dal)
    quart_app.register_blueprint(loyalty_bp)
    quart_app.register_blueprint(loyalty_internal_bp)
    quart_app.config["dal"] = async_dal.dal
    quart_app.config["async_dal"] = async_dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


#: `tests/conftest.py`'s own `auth_headers` fixture (`user_id` defaults to
#: the non-numeric `"u1"`) is used unmodified everywhere except the
#: balance-adjustment routes below, which resolve the caller via
#: `services.current_user.get_current_user_id` (requires a numeric `sub`)
#: -- those tests pass `user_id="1"` explicitly.


@pytest.fixture(autouse=True)
def _service_key_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("SERVICE_API_KEY", SERVICE_API_KEY)


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Default the `community.loyalty` two-gate Feature flag ON for every test in this file."""
    monkeypatch.setattr(loyalty_module, "feature_enabled", AsyncMock(return_value=True))


def _service_headers() -> dict[str, str]:
    return {"X-Service-Key": SERVICE_API_KEY}


async def _post_json(
    client: Any, path: str, *, headers: dict[str, str], body: dict[str, Any]
) -> Any:
    """POST a JSON body via `data=`/`Content-Type`, not the `json=` kwarg.

    `client.post(..., json=body)` intermittently trips a quart-schema/
    pydantic-core test-client bug (`TypeError: 'None' is not an instance
    of 'SchemaSerializer'` inside `TestClientMixin._make_request`'s own
    `model_dump()` call on the outgoing body) against this blueprint's
    internal routes specifically -- same class of bug `services/
    dto_response.py`'s module docstring documents on the response side.
    `data=json.dumps(...)` bypasses `model_dump()` entirely (quart_schema
    only invokes it for the `json=` kwarg) -- same workaround this repo's
    pre-MVP loyalty proxy tests already used for their own POST bodies.
    """
    merged_headers = {**headers, "Content-Type": "application/json"}
    return await client.post(path, data=json_module.dumps(body), headers=merged_headers)


class TestScopeAndTenant:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        response = await client.get(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/config", community_id),
            headers=auth_headers(scope="community.loyalty:write"),
        )
        assert response.status_code == 403

    async def test_wipe_requires_admin_scope(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        response = await client.post(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/wipe", community_id),
            headers=auth_headers(scope="community.loyalty:write"),
        )
        assert response.status_code == 403

    async def test_unknown_community_is_404(self, client: Any, auth_headers: Any) -> None:
        response = await client.get(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/loyalty/config",
            headers=auth_headers(scope="community.loyalty:read"),
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "method,path_suffix,scope",
        [
            ("PUT", "loyalty/config", "community.loyalty:write"),
            ("GET", "loyalty/leaderboard", "community.loyalty:read"),
            ("PUT", f"loyalty/user/{U1_UUID}/balance", "community.loyalty:admin"),
            ("POST", "loyalty/wipe", "community.loyalty:admin"),
            ("GET", "loyalty/stats", "community.loyalty:read"),
        ],
    )
    async def test_remaining_routes_404_on_unknown_community(
        self, client: Any, auth_headers: Any, method: str, path_suffix: str, scope: str
    ) -> None:
        response = await client.open(
            f"/api/v1/admin/{UNKNOWN_PUBLIC_ID}/{path_suffix}",
            method=method,
            headers=auth_headers(scope=scope),
        )
        assert response.status_code == 404

    async def test_feature_disabled_is_402(
        self, client: Any, auth_headers: Any, loyalty_db: Any, monkeypatch: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        monkeypatch.setattr(loyalty_module, "feature_enabled", AsyncMock(return_value=False))
        response = await client.get(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/config", community_id),
            headers=auth_headers(scope="community.loyalty:read"),
        )
        assert response.status_code == 402


class TestConfig:
    async def test_get_config_returns_defaults(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        response = await client.get(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/config", community_id),
            headers=auth_headers(scope="community.loyalty:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["status"] == "success"
        assert body["data"]["currency_name"] == "Points"
        assert body["data"]["enabled"] is True

    async def test_update_config_partial(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        response = await client.put(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/config", community_id),
            headers={
                **auth_headers(scope="community.loyalty:write"),
                "Content-Type": "application/json",
            },
            json={"currency_name": "Gems", "max_balance": 1000},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["currency_name"] == "Gems"
        assert body["data"]["max_balance"] == 1000

    async def test_update_config_empty_body_is_400(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        response = await client.put(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/config", community_id),
            headers={
                **auth_headers(scope="community.loyalty:write"),
                "Content-Type": "application/json",
            },
            json={},
        )
        assert response.status_code == 400


class TestLeaderboardAndBalance:
    async def test_leaderboard_empty(self, client: Any, auth_headers: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        response = await client.get(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/leaderboard", community_id),
            headers=auth_headers(scope="community.loyalty:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["entries"] == []

    async def test_adjust_balance_add_points(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        response = await client.put(
            cpath(
                async_dal,
                "/api/v1/admin/{community_id}/loyalty/user/{U1}/balance",
                community_id,
                U1=U1_UUID,
            ),
            headers={
                **auth_headers(scope="community.loyalty:admin", user_id="1"),
                "Content-Type": "application/json",
            },
            json={"platform": "twitch", "delta": 50, "note": "grant"},
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["balance"] == 50

    async def test_adjust_balance_missing_platform_is_400(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        response = await client.put(
            cpath(
                async_dal,
                "/api/v1/admin/{community_id}/loyalty/user/{U1}/balance",
                community_id,
                U1=U1_UUID,
            ),
            headers={
                **auth_headers(scope="community.loyalty:admin", user_id="1"),
                "Content-Type": "application/json",
            },
            json={"delta": 50},
        )
        assert response.status_code == 400

    async def test_adjust_balance_rejects_negative_overshoot(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        response = await client.put(
            cpath(
                async_dal,
                "/api/v1/admin/{community_id}/loyalty/user/{U1}/balance",
                community_id,
                U1=U1_UUID,
            ),
            headers={
                **auth_headers(scope="community.loyalty:admin", user_id="1"),
                "Content-Type": "application/json",
            },
            json={"platform": "twitch", "delta": -50},
        )
        assert response.status_code == 409


class TestAdjustBalanceMemberUuid:
    """regression: the URL carries the member's public UUID, never a raw platform id."""

    async def _put(
        self, client: Any, auth_headers: Any, loyalty_db: Any, member: str, platform: str = "twitch"
    ) -> Any:
        async_dal, community_id = loyalty_db
        return await client.put(
            cpath(
                async_dal,
                "/api/v1/admin/{community_id}/loyalty/user/{member}/balance",
                community_id,
                member=member,
            ),
            headers={
                **auth_headers(scope="community.loyalty:admin", user_id="1"),
                "Content-Type": "application/json",
            },
            json={"platform": platform, "delta": 10, "note": "n"},
        )

    async def test_unknown_member_uuid_is_404(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        response = await self._put(
            client, auth_headers, loyalty_db, "33333333-3333-4333-8333-333333333333"
        )
        assert response.status_code == 404

    async def test_member_of_another_community_is_404(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        response = await self._put(client, auth_headers, loyalty_db, FOREIGN_UUID)
        assert response.status_code == 404
        assert loyalty_db[0].dal(loyalty_db[0].dal.loyalty_balances).count() == 0

    async def test_platform_without_a_member_row_is_404(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        response = await self._put(client, auth_headers, loyalty_db, U1_UUID, platform="discord")
        assert response.status_code == 404

    async def test_raw_platform_id_in_path_no_longer_routes(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        response = await self._put(client, auth_headers, loyalty_db, "u1")
        assert response.status_code == 404

    async def test_success_keeps_audit_row_keyed_on_resolved_identity(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        response = await self._put(client, auth_headers, loyalty_db, U1_UUID)
        assert response.status_code == 200
        dal = loyalty_db[0].dal
        rows = dal(dal.loyalty_transactions).select()
        assert len(rows) == 1
        assert (rows[0].platform, rows[0].platform_user_id, rows[0].delta) == ("twitch", "u1", 10)
        assert rows[0].actor_platform_user_id == "1"

    async def test_leaderboard_exposes_user_uuid_not_required_for_unlinked(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        await self._put(client, auth_headers, loyalty_db, U1_UUID)
        async_dal, community_id = loyalty_db
        response = await client.get(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/leaderboard", community_id),
            headers=auth_headers(scope="community.loyalty:read"),
        )
        entries = (await response.get_json())["data"]["entries"]
        assert entries[0]["user_uuid"] == U1_UUID


class TestWipeAndStats:
    async def test_wipe_zeroes_balances(
        self, client: Any, auth_headers: Any, loyalty_db: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        await client.put(
            cpath(
                async_dal,
                "/api/v1/admin/{community_id}/loyalty/user/{U1}/balance",
                community_id,
                U1=U1_UUID,
            ),
            headers={
                **auth_headers(scope="community.loyalty:admin", user_id="1"),
                "Content-Type": "application/json",
            },
            json={"platform": "twitch", "delta": 25},
        )
        response = await client.post(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/wipe", community_id),
            headers=auth_headers(scope="community.loyalty:admin"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["affected"] == 1

    async def test_stats_aggregates(self, client: Any, auth_headers: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        await client.put(
            cpath(
                async_dal,
                "/api/v1/admin/{community_id}/loyalty/user/{U1}/balance",
                community_id,
                U1=U1_UUID,
            ),
            headers={
                **auth_headers(scope="community.loyalty:admin", user_id="1"),
                "Content-Type": "application/json",
            },
            json={"platform": "twitch", "delta": 40},
        )
        response = await client.get(
            cpath(async_dal, "/api/v1/admin/{community_id}/loyalty/stats", community_id),
            headers=auth_headers(scope="community.loyalty:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["total_users"] == 1
        assert body["data"]["total_currency"] == 40


class TestInternalRoutes:
    async def test_bad_service_key_is_401(self, client: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        response = await client.get(
            f"/api/v1/internal/loyalty/balance?community_id={community_id}&platform=twitch&platform_user_id=u1",
            headers={"X-Service-Key": "wrong"},
        )
        assert response.status_code == 401

    async def test_unknown_community_is_404(self, client: Any) -> None:
        response = await client.get(
            "/api/v1/internal/loyalty/balance?community_id=9999&platform=twitch&platform_user_id=u1",
            headers=_service_headers(),
        )
        assert response.status_code == 404

    async def test_get_balance(self, client: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        response = await client.get(
            f"/api/v1/internal/loyalty/balance?community_id={community_id}&platform=twitch&platform_user_id=u1",
            headers=_service_headers(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["balance"] == 0

    async def test_leaderboard(self, client: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        response = await client.get(
            f"/api/v1/internal/loyalty/leaderboard?community_id={community_id}&limit=5",
            headers=_service_headers(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["entries"] == []

    async def test_list_items(self, client: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        response = await client.get(
            f"/api/v1/internal/loyalty/items?community_id={community_id}",
            headers=_service_headers(),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["items"] == []

    async def test_earn_credits_points(self, client: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        response = await _post_json(
            client,
            "/api/v1/internal/loyalty/earn",
            headers=_service_headers(),
            body={
                "community_id": community_id,
                "platform": "twitch",
                "platform_user_id": "u1",
                "kind": "earn_chat",
                "points": 5,
                "ref": "chat-msg",
            },
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["data"]["balance"]["balance"] == 5
        assert body["data"]["applied_delta"] == 5

    async def test_earn_gated_by_feature_flag(
        self, client: Any, loyalty_db: Any, monkeypatch: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        monkeypatch.setattr(loyalty_module, "feature_enabled", AsyncMock(return_value=False))
        response = await _post_json(
            client,
            "/api/v1/internal/loyalty/earn",
            headers=_service_headers(),
            body={
                "community_id": community_id,
                "platform": "twitch",
                "platform_user_id": "u1",
                "kind": "earn_chat",
                "points": 5,
            },
        )
        assert response.status_code == 402

    async def test_adjust_via_internal_route(self, client: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        response = await _post_json(
            client,
            "/api/v1/internal/loyalty/adjust",
            headers=_service_headers(),
            body={
                "community_id": community_id,
                "platform": "twitch",
                "platform_user_id": "u1",
                "delta": -10,
                "actor_platform_user_id": "mod1",
                "note": "mod deduction",
            },
        )
        # No prior balance -- default rejection floors nowhere, insufficient points.
        assert response.status_code == 409
        body = await response.get_json()
        assert body["error"]["message"] == "insufficient points"

    async def test_redeem_not_enough_points_message(self, client: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        await _post_json(
            client,
            "/api/v1/internal/loyalty/earn",
            headers=_service_headers(),
            body={
                "community_id": community_id,
                "platform": "twitch",
                "platform_user_id": "u1",
                "kind": "earn_chat",
                "points": 5,
            },
        )
        response = await _post_json(
            client,
            "/api/v1/internal/loyalty/redeem",
            headers=_service_headers(),
            body={
                "community_id": community_id,
                "platform": "twitch",
                "platform_user_id": "u1",
                "sku": "nonexistent",
            },
        )
        assert response.status_code == 409
        body = await response.get_json()
        assert body["error"]["message"] == "unknown item 'nonexistent'"

    async def test_redeem_gated_by_feature_flag(
        self, client: Any, loyalty_db: Any, monkeypatch: Any
    ) -> None:
        async_dal, community_id = loyalty_db
        monkeypatch.setattr(loyalty_module, "feature_enabled", AsyncMock(return_value=False))
        response = await _post_json(
            client,
            "/api/v1/internal/loyalty/redeem",
            headers=_service_headers(),
            body={
                "community_id": community_id,
                "platform": "twitch",
                "platform_user_id": "u1",
                "sku": "hat",
            },
        )
        assert response.status_code == 402

    async def test_redeem_missing_fields_is_400(self, client: Any, loyalty_db: Any) -> None:
        async_dal, community_id = loyalty_db
        response = await _post_json(
            client,
            "/api/v1/internal/loyalty/redeem",
            headers=_service_headers(),
            body={"community_id": community_id},
        )
        assert response.status_code == 400


#: Every JSON response read here must carry community ids as UUID strings (conftest guard).
UUID_COMMUNITY_RESPONSES = True


pytestmark = pytest.mark.usefixtures("bypass_route_guards")
