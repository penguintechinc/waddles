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
from tests.community_ids import UNKNOWN_PUBLIC_ID, cpath, install_community_ids, public_id
from tests.entity_ids import pid


@pytest.fixture
def app(guild_pairing_db: Any) -> Quart:
    dal, _community_id, _tenant_id, _global_id = guild_pairing_db
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = dal
    install_community_ids(quart_app, dal)
    quart_app.register_blueprint(guild_pairing_bp)
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import blueprints.v1.guild_pairing as guild_pairing_module

    monkeypatch.setattr(guild_pairing_module, "feature_enabled", AsyncMock(return_value=True))
    # The Discord membership check is covered by test_discord_server_install_pg_integration.py.
    monkeypatch.setattr(
        guild_pairing_module.roles_svc, "verify_bot_in_guild", AsyncMock(return_value=None)
    )
    # Install-grant authority gate is covered on real Postgres in the same PG suite.
    monkeypatch.setattr(
        guild_pairing_module.server_install_svc,
        "community_has_install_grant",
        lambda *_a, **_k: True,
    )


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
    client: Any, auth_headers: Any, dal: Any, community_id: int, **overrides: Any
) -> Any:
    body = {
        "discord_guild_id": "123456789012345678",  # gitleaks:allow - fake snowflake, not a secret
        "direction": "bidirectional",
        "role_name_prefix": "[ACSEA]",
    }
    body.update(overrides)
    return await client.post(
        cpath(dal, "/api/v1/communities/{community_id}/guild-pairings", community_id),
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
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await client.get(
            cpath(dal, "/api/v1/communities/{community_id}/guild-pairings", community_id),
            headers=auth_headers(scope="community.guild_pairing:write"),
        )
        assert response.status_code == 403

    async def test_empty_list_shape(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await client.get(
            cpath(dal, "/api/v1/communities/{community_id}/guild-pairings", community_id),
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {"success": True, "pairings": []}

    async def test_unknown_community_is_error(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        response = await client.get(
            f"/api/v1/communities/{UNKNOWN_PUBLIC_ID}/guild-pairings",
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
            cpath(dal, "/api/v1/communities/{community_id}/guild-pairings", other_community_id),
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert response.status_code == 404


class TestCreatePairing:
    async def test_create_returns_201_with_pairing_fields(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await _create_pairing(client, auth_headers, dal, community_id)
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
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await client.post(
            cpath(dal, "/api/v1/communities/{community_id}/guild-pairings", community_id),
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
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await _create_pairing(
            client, auth_headers, dal, community_id, direction="sideways"
        )
        assert response.status_code == 400

    async def test_duplicate_pairing_is_409(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        first = await _create_pairing(client, auth_headers, dal, community_id)
        assert first.status_code == 201
        second = await _create_pairing(client, auth_headers, dal, community_id)
        assert second.status_code == 409


class TestUpdateAndDeletePairing:
    async def test_update_sync_enabled(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        created = await _create_pairing(client, auth_headers, dal, community_id)
        pairing_id = (await created.get_json())["pairing"]["id"]

        response = await client.patch(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}",
                community_id,
                pairing_id=pairing_id,
            ),
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
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        response = await client.patch(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/00000000-0000-4000-8000-0000000000ff",
                community_id,
            ),
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
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        created = await _create_pairing(client, auth_headers, dal, community_id)
        pairing_id = (await created.get_json())["pairing"]["id"]

        delete_headers = auth_headers(scope="community.guild_pairing:write")
        first = await client.delete(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}",
                community_id,
                pairing_id=pairing_id,
            ),
            headers=delete_headers,
        )
        assert first.status_code == 204
        second = await client.delete(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}",
                community_id,
                pairing_id=pairing_id,
            ),
            headers=delete_headers,
        )
        assert second.status_code == 404


class TestRoleSyncBindings:
    async def _pairing_id(self, client: Any, auth_headers: Any, dal: Any, community_id: int) -> str:
        created = await _create_pairing(client, auth_headers, dal, community_id)
        return str((await created.get_json())["pairing"]["id"])

    async def test_create_and_list_binding(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, dal, community_id)

        create_response = await client.post(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
                community_id,
                pairing_id=pairing_id,
            ),
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
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
                community_id,
                pairing_id=pairing_id,
            ),
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
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, dal, community_id)

        create_response = await client.post(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
                community_id,
                pairing_id=pairing_id,
            ),
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
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
                community_id,
                pairing_id=pairing_id,
            ),
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        body = await list_response.get_json()
        assert body["bindings"][0]["sync_scope"] == "community_role"
        assert body["bindings"][0]["community_role"] == "vip"

    async def test_community_role_missing_role_value_is_400(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, dal, community_id)

        response = await client.post(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
                community_id,
                pairing_id=pairing_id,
            ),
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
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, dal, community_id)

        response = await client.post(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
                community_id,
                pairing_id=pairing_id,
            ),
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
        dal, community_id, _tenant_id, _global_id = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, dal, community_id)

        create_response = await client.post(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
                community_id,
                pairing_id=pairing_id,
            ),
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps({"sync_scope": "moderator", "discord_role_id": "1"}),
        )
        binding_id = (await create_response.get_json())["binding"]["id"]

        delete_response = await client.delete(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings/{binding_id}",
                community_id,
                pairing_id=pairing_id,
                binding_id=binding_id,
            ),
            headers=auth_headers(scope="community.guild_pairing:write"),
        )
        assert delete_response.status_code == 204


class TestPrivilegeHardening:
    """S1/S3 regressions at the HTTP surface (role-sync privilege hardening, v3.0)."""

    async def _pairing_id(self, client: Any, auth_headers: Any, dal: Any, community_id: int) -> str:
        response = await _create_pairing(client, auth_headers, dal, community_id)
        assert response.status_code == 201
        return str((await response.get_json())["pairing"]["id"])

    async def _post_binding(
        self,
        client: Any,
        auth_headers: Any,
        dal: Any,
        community_id: int,
        pairing_id: str,
        body: Any,
    ) -> Any:
        return await client.post(
            cpath(
                dal,
                "/api/v1/communities/{community_id}/guild-pairings/{pairing_id}/role-bindings",
                community_id,
                pairing_id=pairing_id,
            ),
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
        dal, community_id, _t, _g = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, dal, community_id)
        response = await self._post_binding(
            client,
            auth_headers,
            dal,
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
        dal, community_id, _t, _g = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, dal, community_id)
        response = await self._post_binding(
            client,
            auth_headers,
            dal,
            community_id,
            pairing_id,
            {**scope_body, "discord_role_id": _PRIVILEGED_DISCORD_ROLE},
        )
        assert response.status_code == 422

    async def test_pairing_without_guild_control_proof_is_refused(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any, _discord_fakes: AsyncMock
    ) -> None:
        from services.errors import ApiError

        dal, community_id, _t, _g = guild_pairing_db
        _discord_fakes.side_effect = ApiError("no", 403, "guild_control_insufficient")
        response = await _create_pairing(client, auth_headers, dal, community_id)
        assert response.status_code == 403
        list_response = await client.get(
            cpath(dal, "/api/v1/communities/{community_id}/guild-pairings", community_id),
            headers=auth_headers(scope="community.guild_pairing:read"),
        )
        assert (await list_response.get_json())["pairings"] == []

    async def test_new_pairing_records_ownership_and_mod_propagation_defaults_off(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, _t, _g = guild_pairing_db
        pairing_id = await self._pairing_id(client, auth_headers, dal, community_id)
        row = dal(dal.guild_tenant_pairings.public_id == pairing_id).select().first()
        assert row.ownership_verified_at is not None
        assert row.propagate_moderators is False


#: Every JSON response read here must carry community ids as UUID strings (conftest guard).
UUID_COMMUNITY_RESPONSES = True


class TestUuidPathIds:
    """`<puuid:>` ids (pairing, binding, mirror mapping): own UUID resolves, else 404."""

    @staticmethod
    def _seed(dal: Any, community_id: int, tenant_id: int) -> dict[str, Any]:
        """A second community in the SAME tenant owning pairing/binding/mirror rows."""
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        other = dal.communities.insert(tenant_id=tenant_id)
        pairing = dal.guild_tenant_pairings.insert(
            community_id=other,
            discord_guild_id="555",
            direction="bidirectional",
            sync_enabled=False,
            role_name_prefix="[T]",
            created_at=now,
            updated_at=now,
        )
        binding = dal.community_role_sync_bindings.insert(
            pairing_id=pairing,
            sync_scope="subscriber_tier",
            subscriber_tier=1,
            discord_role_id="1",
            created_at=now,
            updated_at=now,
        )
        mapping = dal.discord_role_mirror_mappings.insert(
            community_id=other,
            source_guild_id="555",
            source_role_id="1",
            target_guild_id="556",
            target_role_id="2",
            created_at=now,
        )
        dal.commit()
        return {"pairing": pairing, "binding": binding, "mapping": mapping}

    def _routes(self, dal: Any, ids: dict[str, Any]) -> list[tuple[str, str, str]]:
        """`(method, path-after-community, entity)` for every converted route of this blueprint."""
        p = pid(dal, "guild_tenant_pairings", ids["pairing"])
        b = pid(dal, "community_role_sync_bindings", ids["binding"])
        m = pid(dal, "discord_role_mirror_mappings", ids["mapping"])
        return [
            ("patch", f"/guild-pairings/{p}", "pairing"),
            ("delete", f"/guild-pairings/{p}", "pairing"),
            ("get", f"/guild-pairings/{p}/role-bindings", "pairing"),
            ("delete", f"/guild-pairings/{p}/role-bindings/{b}", "pairing"),
            ("delete", f"/discord/role-mirrors/{m}", "mapping"),
        ]

    async def _call(
        self, client: Any, auth_headers: Any, method: str, url: str, scope_kind: str = "write"
    ) -> Any:
        return await getattr(client, method)(
            url,
            headers={
                **auth_headers(scope=f"community.guild_pairing:{scope_kind}"),
                "Content-Type": "application/json",
            },
            **({"data": "{}"} if method == "patch" else {}),
        )

    async def test_foreign_community_uuids_are_the_same_404_as_unknown_ones(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, tenant_id, _global_id = guild_pairing_db
        ids = self._seed(dal, community_id, tenant_id)
        base = f"/api/v1/communities/{public_id(dal, community_id)}"
        unknown = "00000000-0000-4000-8000-0000000000ff"
        for method, path, _entity in self._routes(dal, ids):
            kind = "read" if method == "get" else "write"
            foreign = await self._call(client, auth_headers, method, base + path, kind)
            # the same route with every id swapped for one nobody owns
            masked = path
            for real in (
                pid(dal, "guild_tenant_pairings", ids["pairing"]),
                pid(dal, "community_role_sync_bindings", ids["binding"]),
                pid(dal, "discord_role_mirror_mappings", ids["mapping"]),
            ):
                masked = masked.replace(real, unknown)
            missing = await self._call(client, auth_headers, method, base + masked, kind)
            assert foreign.status_code == missing.status_code == 404, (method, path)
            f_body, m_body = await foreign.get_json(), await missing.get_json()
            for body in (f_body, m_body):
                body["error"].pop("timestamp", None)  # the only per-request difference
            assert f_body == m_body, (method, path)
        # nothing of the other community was touched
        assert dal(dal.guild_tenant_pairings.id == ids["pairing"]).count() == 1
        assert dal(dal.community_role_sync_bindings.id == ids["binding"]).count() == 1
        assert dal(dal.discord_role_mirror_mappings.id == ids["mapping"]).count() == 1

    async def test_numeric_ids_in_the_path_do_not_route(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, tenant_id, _global_id = guild_pairing_db
        base = f"/api/v1/communities/{public_id(dal, community_id)}"
        for method, path in (
            ("patch", "/guild-pairings/1"),
            ("delete", "/guild-pairings/1"),
            ("get", "/guild-pairings/1/role-bindings"),
            ("delete", "/guild-pairings/1/role-bindings/1"),
            ("delete", "/discord/role-mirrors/1"),
        ):
            kind = "read" if method == "get" else "write"
            response = await self._call(client, auth_headers, method, base + path, kind)
            assert response.status_code == 404, (method, path)

    async def test_own_community_uuids_resolve_and_responses_carry_uuids(
        self, client: Any, auth_headers: Any, guild_pairing_db: Any
    ) -> None:
        dal, community_id, tenant_id, _global_id = guild_pairing_db
        created = await _create_pairing(client, auth_headers, dal, community_id)
        pairing = (await created.get_json())["pairing"]
        assert pairing["id"] == pid(
            dal, "guild_tenant_pairings", dal(dal.guild_tenant_pairings.id > 0).select().first().id
        )
        base = f"/api/v1/communities/{public_id(dal, community_id)}/guild-pairings/{pairing['id']}"
        made = await client.post(
            base + "/role-bindings",
            headers={
                **auth_headers(scope="community.guild_pairing:write"),
                "Content-Type": "application/json",
            },
            data=json_module.dumps(
                {"sync_scope": "subscriber_tier", "subscriber_tier": 1, "discord_role_id": "42"}
            ),
        )
        assert made.status_code == 201
        binding = (await made.get_json())["binding"]
        assert binding["pairing_id"] == pairing["id"]  # the pairing's UUID, not its numeric id
        deleted = await self._call(
            client, auth_headers, "delete", f"{base}/role-bindings/{binding['id']}"
        )
        assert deleted.status_code == 204
        assert (await self._call(client, auth_headers, "delete", base)).status_code == 204


pytestmark = pytest.mark.usefixtures("bypass_route_guards")
