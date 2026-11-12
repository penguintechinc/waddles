"""`blueprints/v1/guild_pairing.py` -- auth, tenant isolation, CRUD happy+error paths.

Same auth-surface coverage style as `test_community_activity.py`/
`test_community_connections_api.py`: `tenant_middleware` + `require_scope`
scope enforcement and response shape for the authenticated path (a missing
bearer token is already covered port-wide by `test_community_auth_bypass.py`).
"""

from __future__ import annotations

import json as json_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.guild_pairing import guild_pairing_bp


@pytest.fixture
def app(guild_pairing_db: Any) -> Quart:
    dal, _community_id, _tenant_id, _global_id = guild_pairing_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(guild_pairing_bp)
    quart_app.config["dal"] = dal
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import blueprints.v1.guild_pairing as guild_pairing_module

    monkeypatch.setattr(guild_pairing_module, "feature_enabled", AsyncMock(return_value=True))


#: Discord role id the fake guild reports as privileged (e.g. carries MANAGE_ROLES).
_PRIVILEGED_DISCORD_ROLE = "777"


@pytest.fixture(autouse=True)
def _discord_fakes(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Ownership proof passes and the guild's roles are non-privileged unless a test says so."""
    from services import discord_guild_roles, guild_ownership
    from services.discord_guild_roles import DiscordRole

    proof = AsyncMock(return_value=None)
    monkeypatch.setattr(guild_ownership, "verify_guild_control", proof)

    async def _roles(dal: Any, community_id: int, *, guild_id: str | None = None) -> list[Any]:
        ids = ["1", "2", "42", "901", "999", _PRIVILEGED_DISCORD_ROLE]
        return [
            DiscordRole(
                id=i,
                name=f"r{i}",
                color=0,
                position=0,
                privileged=i == _PRIVILEGED_DISCORD_ROLE,
            )
            for i in ids
        ]

    monkeypatch.setattr(discord_guild_roles, "list_guild_roles", _roles)
    return proof


async def _create_pairing(
    client: Any, auth_headers: Any, community_id: int, **overrides: Any
) -> Any:
    body = {
        "discord_guild_id": "123456789012345678",  # gitleaks:allow - fake snowflake, not a secret
        "direction": "bidirectional",
        "role_name_prefix": "[ACSEA]",
    }
    body.update(overrides)
    return await client.post(
        f"/api/v1/communities/{community_id}/guild-pairings",
        headers={
            # `get_current_user_id` requires an int-parseable `sub` claim --
            # `auth_headers`' own default (`user_id="u1"`) isn't one.
            **auth_headers(scope="community.guild_pairing:write", user_id="1"),
            "Content-Type": "application/json",
        },
        data=json_module.dumps(body),
    )


class TestListPairings:
    async def test_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await client.get(
            f"/api/v1/communities/{community_id}/guild-pairings",
            headers=auth_headers(scope="community.guild_pairing:write"),
        )
        assert response.status_code == 403

    async def test_empty_list_shape(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await client.get(
            f"/api/v1/communities/{community_id}/guild-pairings",
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "pairings": []}

    async def test_unknown_community_is_error(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        response = await client.get(
            "/api/v1/communities/999999/guild-pairings",
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert response.status_code == 404

    async def test_cross_tenant_community_is_rejected(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, _community_id, _tenant_id, _global_id = guild_pairing_db
        other_tenant_id = dal.tenants.insert(slug="other-corp", is_active=True, is_global=False)
        other_community_id = dal.communities.insert(name="other", tenant_id=other_tenant_id)
        dal.commit()

        response = await client.get(
            f"/api/v1/communities/{other_community_id}/guild-pairings",
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert response.status_code == 404


class TestCreatePairing:
    async def test_create_returns_201_with_pairing_fields(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await _create_pairing(client, auth_headers, community_id)
        assert response.status_code == 201
        body = await response.get_json()
        assert body["success"] is True
        assert set(body["pairing"].keys()) == {
            "id",
            "community_id",
            "discord_guild_id",
            "direction",
            "sync_enabled",
            "role_name_prefix",
            "event_sync_enabled",
            "propagate_moderators",
            "created_by_user_id",
            "created_at",
            "updated_at",
        }
        assert body["pairing"]["sync_enabled"] is False
        assert body["pairing"]["event_sync_enabled"] is False

    async def test_create_wrong_scope_is_403(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await client.post(
            f"/api/v1/communities/{community_id}/guild-pairings",
            headers={
                **auth_headers(scope="community.guild_pairing:read"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {"discord_guild_id": "1", "direction": "bidirectional", "role_name_prefix": "[X]"}
            ),
        )
        assert response.status_code == 403

    async def test_invalid_direction_is_400(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await _create_pairing(client, auth_headers, community_id, direction="sideways")
        assert response.status_code == 400

    async def test_duplicate_pairing_is_409(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        first = await _create_pairing(client, auth_headers, community_id)
        assert first.status_code == 201
        second = await _create_pairing(client, auth_headers, community_id)
        assert second.status_code == 409


class TestUpdateAndDeletePairing:
    async def test_update_sync_enabled(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        created = await _create_pairing(client, auth_headers, community_id)
        pairing_id = (await created.get_json())["pairing"]["id"]

        response = await client.patch(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"sync_enabled": True}),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["pairing"]["sync_enabled"] is True

    async def test_update_unknown_pairing_is_error(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await client.patch(
            f"/api/v1/communities/{community_id}/guild-pairings/999999",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"sync_enabled": True}),
        )
        assert response.status_code == 404

    async def test_delete_pairing_is_204_then_404_on_redelete(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        created = await _create_pairing(client, auth_headers, community_id)
        pairing_id = (await created.get_json())["pairing"]["id"]

        delete_headers = auth_headers(scope="community.guild_pairing:write")
        first = await client.delete(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}",
            headers=delete_headers,
        )
        assert first.status_code == 204
        second = await client.delete(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}",
            headers=delete_headers,
        )
        assert second.status_code == 404


class TestRoleSyncBindings:
    async def _pairing_id(self, client: Any, auth_headers: Any, community_id: int) -> int:
        created = await _create_pairing(client, auth_headers, community_id)
        return int((await created.get_json())["pairing"]["id"])

    async def test_create_and_list_binding(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, community_id)

        create_response = await client.post(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {"sync_scope": "subscriber_tier", "subscriber_tier": 1, "discord_role_id": "42"}
            ),
        )
        assert create_response.status_code == 201

        list_response = await client.get(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert list_response.status_code == 200
        body = await list_response.get_json()
        assert len(body["bindings"]) == 1
        assert body["bindings"][0]["subscriber_tier"] == 1

    async def test_create_and_list_community_role_binding(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        """The hub-api endpoint the webui maps Discord roles -> community scopes through."""
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, community_id)

        create_response = await client.post(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {
                    "sync_scope": "community_role",
                    "discord_role_id": "901",
                    "community_role": "vip",
                }
            ),
        )
        assert create_response.status_code == 201
        created_body = await create_response.get_json()
        assert created_body["binding"]["community_role"] == "vip"

        list_response = await client.get(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        body = await list_response.get_json()
        assert body["bindings"][0]["sync_scope"] == "community_role"
        assert body["bindings"][0]["community_role"] == "vip"

    async def test_community_role_missing_role_value_is_400(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, community_id)

        response = await client.post(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"sync_scope": "community_role", "discord_role_id": "901"}),
        )
        assert response.status_code == 400

    async def test_invalid_scope_tier_combo_is_400(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, community_id)

        response = await client.post(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {"sync_scope": "moderator", "subscriber_tier": 1, "discord_role_id": "1"}
            ),
        )
        assert response.status_code == 400

    async def test_delete_binding(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        _, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, community_id)

        create_response = await client.post(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"sync_scope": "moderator", "discord_role_id": "1"}),
        )
        binding_id = (await create_response.get_json())["binding"]["id"]

        delete_response = await client.delete(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings/{binding_id}",
            headers=auth_headers(scope="community.guild_pairing:write"),
        )
        assert delete_response.status_code == 204


class TestPrivilegeHardening:
    """S1/S3 regressions at the HTTP surface (role-sync privilege hardening, v3.0)."""

    async def _pairing_id(self, client: Any, auth_headers: Any, community_id: int) -> int:
        response = await _create_pairing(client, auth_headers, community_id)
        assert response.status_code == 201
        return int((await response.get_json())["pairing"]["id"])

    async def _post_binding(
        self, client: Any, auth_headers: Any, community_id: int, pairing_id: int, body: Any
    ) -> Any:
        return await client.post(
            f"/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(body),
        )

    @pytest.mark.parametrize("role", ["community-admin", "moderator"])
    async def test_privileged_inbound_binding_rejected_422(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any, role: str
    ) -> None:
        _, community_id, _t, _g = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, community_id)
        response = await self._post_binding(
            client,
            auth_headers,
            community_id,
            pairing_id,
            {"sync_scope": "community_role", "discord_role_id": "901", "community_role": role},
        )
        assert response.status_code == 422
        assert (await response.get_json())["error"]["code"] == "privileged_target_role"

    @pytest.mark.parametrize(
        "scope_body",
        [{"sync_scope": "moderator"}, {"sync_scope": "subscriber_tier", "subscriber_tier": 1}],
    )
    async def test_outbound_binding_to_privileged_discord_role_rejected_422(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any, scope_body: Any
    ) -> None:
        _, community_id, _t, _g = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, community_id)
        response = await self._post_binding(
            client,
            auth_headers,
            community_id,
            pairing_id,
            {**scope_body, "discord_role_id": _PRIVILEGED_DISCORD_ROLE},
        )
        assert response.status_code == 422

    async def test_pairing_without_guild_control_proof_is_refused(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any, _discord_fakes: AsyncMock
    ) -> None:
        from services.errors import ApiError

        _, community_id, _t, _g = guild_pairing_db
        _discord_fakes.side_effect = ApiError("no", 403, "guild_control_insufficient")
        response = await _create_pairing(client, auth_headers, community_id)
        assert response.status_code == 403
        list_response = await client.get(
            f"/api/v1/communities/{community_id}/guild-pairings",
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert (await list_response.get_json())["pairings"] == []

    async def test_new_pairing_records_ownership_and_mod_propagation_defaults_off(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, _t, _g = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, community_id)
        row = dal(dal.guild_tenant_pairings.id == pairing_id).select().first()
        assert row.ownership_verified_at is not None
        assert row.propagate_moderators is False
