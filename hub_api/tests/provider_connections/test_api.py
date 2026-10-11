"""The HTTP surface: auth chain order, feature/tier gates, DTO shapes, error mapping, internal API."""

from __future__ import annotations

import json
from typing import Any

import pytest
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer

from services.provider_connections.store import SealedSecrets  # noqa: F401 - import smoke
from tests.conftest import make_user_token
from tests.provider_connections.conftest import (
    BOT,
    CHANNEL_ID,
    CHANNEL_LOGIN,
    OTHER_CHANNEL_LOGIN,
    SERVICE_AUDIENCE,
    SYSTEM_SERVICE_ID,
    FakeClock,
    admin_headers,
)
from tests.provider_connections.fake_twitch import FakeTwitch
from tests.provider_connections.support import raw_rows, set_column
from tests.sso.conftest import Entitlements

ADMIN_FIELDS = {
    "id",
    "provider",
    "resourceType",
    "status",
    "statusReason",
    "enabled",
    "access",
    "usable",
    "shared",
    "hasRefreshToken",
    "expiresAt",
    "capabilities",
    "metadata",
    "createdAt",
    "updatedAt",
}


def _base(db: Any, community_id: int | None = None) -> str:
    return f"/api/v1/communities/{community_id or db.seed.community_id}/provider-connections"


def _body(tokens: tuple[str, str | None], **overrides: Any) -> str:
    body: dict[str, Any] = {
        "provider": "twitch",
        "credentials": {"access_token": tokens[0], "refresh_token": tokens[1]},
        "metadata": {"channel_login": CHANNEL_LOGIN},
    }
    body.update(overrides)
    return json.dumps(body)


async def _create(client: Any, db: Any, tokens: tuple[str, str | None], **overrides: Any) -> Any:
    return await client.post(_base(db), headers=admin_headers(db), data=_body(tokens, **overrides))


class TestDescriptors:
    async def test_descriptor_is_everything_a_generic_ui_needs(
        self, client: Any, pc_db: Any
    ) -> None:
        response = await client.get("/api/v1/connection-providers", headers=admin_headers(pc_db))
        assert response.status_code == 200
        body = await response.get_json()
        assert set(body) == {"success", "providers"}
        (twitch,) = body["providers"]
        assert set(twitch) == {
            "provider",
            "displayName",
            "installKind",
            "resourceType",
            "capabilities",
            "requiredScopes",
            "refreshable",
            "credentialFields",
            "metadataFields",
        }
        assert twitch["provider"] == "twitch" and twitch["installKind"] == "credentials"
        assert twitch["capabilities"] == ["action_relay", "chat_ingest"]
        assert twitch["requiredScopes"] == ["chat:read"]
        assert [f["name"] for f in twitch["credentialFields"]] == ["access_token", "refresh_token"]
        assert [f["required"] for f in twitch["credentialFields"]] == [True, False]
        (channel,) = twitch["metadataFields"]
        assert set(channel) == {"name", "required", "description", "pattern", "maxLength"}
        assert channel["name"] == "channel_login" and channel["required"] is True

    async def test_descriptors_need_auth_scope_and_the_feature(
        self, client: Any, pc_db: Any, entitlements: Entitlements
    ) -> None:
        assert (await client.get("/api/v1/connection-providers")).status_code == 401
        no_scope = admin_headers(pc_db, scope="*:read")
        assert (
            await client.get("/api/v1/connection-providers", headers=no_scope)
        ).status_code == 403
        entitlements.flag_off("waddles.integrations.provider_connections")
        gated = await client.get("/api/v1/connection-providers", headers=admin_headers(pc_db))
        assert gated.status_code == 402


class TestAuthChain:
    async def test_unauthenticated(self, client: Any, pc_db: Any) -> None:
        for method in ("get", "post", "patch", "delete"):
            response = await getattr(client, method)(
                _base(pc_db) + ("/1" if method in ("patch", "delete") else "")
            )
            assert response.status_code == 401, method

    async def test_the_universal_read_wildcard_is_not_enough(self, client: Any, pc_db: Any) -> None:
        """Every session carries ``*:read``; ``connections:admin`` must not be satisfiable by it."""
        response = await client.get(_base(pc_db), headers=admin_headers(pc_db, scope="*:read"))
        assert response.status_code == 403

    async def test_a_scoped_token_for_an_unrelated_admin_scope_is_refused(
        self, client: Any, pc_db: Any
    ) -> None:
        response = await client.get(
            _base(pc_db), headers=admin_headers(pc_db, scope="auth.sso:admin")
        )
        assert response.status_code == 403

    async def test_global_admin_wildcard_satisfies_the_scope(self, client: Any, pc_db: Any) -> None:
        pc_db.dal.hub_users.insert(username="root", is_super_admin=True)
        pc_db.dal.commit()
        root_id = pc_db.dal(pc_db.dal.hub_users.username == "root").select().first().id
        headers = admin_headers(pc_db, scope="*:read *:admin", user_id=root_id)
        # Scope passes via *:admin; the per-community DB check then decides (not a tenant admin,
        # not a member) -> 403. Possession of a broad scope is never sufficient on its own.
        assert (await client.get(_base(pc_db), headers=headers)).status_code == 403

    async def test_scope_holder_who_does_not_administer_the_community_is_refused(
        self, client: Any, pc_db: Any
    ) -> None:
        pc_db.dal.hub_users.insert(username="stranger")
        pc_db.dal.commit()
        stranger = pc_db.dal(pc_db.dal.hub_users.username == "stranger").select().first().id
        response = await client.get(_base(pc_db), headers=admin_headers(pc_db, user_id=stranger))
        assert response.status_code == 403

    async def test_a_plain_member_is_refused_even_with_the_scope(
        self, client: Any, pc_db: Any
    ) -> None:
        pc_db.dal.hub_users.insert(username="member")
        pc_db.dal.commit()
        member = pc_db.dal(pc_db.dal.hub_users.username == "member").select().first().id
        pc_db.dal.community_members.insert(
            community_id=pc_db.seed.community_id, user_id=str(member), role="member", is_active=True
        )
        pc_db.dal.commit()
        response = await client.get(_base(pc_db), headers=admin_headers(pc_db, user_id=member))
        assert response.status_code == 403

    async def test_a_community_admin_with_the_scope_is_allowed_without_being_tenant_admin(
        self, client: Any, pc_db: Any
    ) -> None:
        pc_db.dal.hub_users.insert(username="comm-admin")
        pc_db.dal.commit()
        user = pc_db.dal(pc_db.dal.hub_users.username == "comm-admin").select().first().id
        pc_db.dal.community_members.insert(
            community_id=pc_db.seed.community_id,
            user_id=str(user),
            role="moderator",
            is_active=True,
            claims_cache={"scopes": ["community:manage_channels"]},
        )
        pc_db.dal.commit()
        ok = await client.get(_base(pc_db), headers=admin_headers(pc_db, user_id=user))
        assert ok.status_code == 200
        # ...but only for THEIR community.
        other = await client.get(
            _base(pc_db, pc_db.seed.second_community_id), headers=admin_headers(pc_db, user_id=user)
        )
        assert other.status_code == 403

    async def test_a_community_admin_without_the_tenant_scope_is_refused(
        self, client: Any, pc_db: Any
    ) -> None:
        pc_db.dal.hub_users.insert(username="comm-admin2")
        pc_db.dal.commit()
        user = pc_db.dal(pc_db.dal.hub_users.username == "comm-admin2").select().first().id
        pc_db.dal.community_members.insert(
            community_id=pc_db.seed.community_id,
            user_id=str(user),
            role="moderator",
            is_active=True,
            claims_cache={"scopes": ["community:manage_channels"]},
        )
        pc_db.dal.commit()
        response = await client.get(
            _base(pc_db), headers=admin_headers(pc_db, scope="*:read", user_id=user)
        )
        assert response.status_code == 403

    async def test_a_tenant_owner_cannot_reach_another_tenants_community(
        self, client: Any, pc_db: Any
    ) -> None:
        response = await client.get(
            _base(pc_db, pc_db.seed.other_community_id), headers=admin_headers(pc_db)
        )
        assert response.status_code == 403

    async def test_a_token_for_the_other_tenant_cannot_reach_this_tenants_community(
        self, client: Any, pc_db: Any
    ) -> None:
        owner_of_other = admin_headers(pc_db, tenant="other-corp")
        response = await client.get(_base(pc_db), headers=owner_of_other)
        assert response.status_code == 403

    async def test_the_tenant_never_comes_from_the_request(
        self, client: Any, pc_db: Any, bot_tokens: Any
    ) -> None:
        response = await client.post(
            _base(pc_db, pc_db.seed.other_community_id),
            headers=admin_headers(pc_db),
            data=_body(bot_tokens, tenant_id=pc_db.seed.other_tenant_id),
        )
        assert response.status_code in (400, 403)  # unknown field rejected / community refused
        assert await raw_rows(pc_db, "platform_connections") == []


class TestFeatureGates:
    async def test_flag_off_is_a_402_on_every_admin_route(
        self, client: Any, pc_db: Any, entitlements: Entitlements, bot_tokens: Any
    ) -> None:
        entitlements.flag_off("waddles.integrations.provider_connections")
        headers = admin_headers(pc_db)
        assert (await client.get(_base(pc_db), headers=headers)).status_code == 402
        assert (
            await client.post(_base(pc_db), headers=headers, data=_body(bot_tokens))
        ).status_code == 402
        assert (await client.get(_base(pc_db) + "/1", headers=headers)).status_code == 402
        assert (
            await client.patch(_base(pc_db) + "/1", headers=headers, data="{}")
        ).status_code == 402
        assert (await client.delete(_base(pc_db) + "/1", headers=headers)).status_code == 402
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_the_free_tier_is_denied_even_with_the_flag_on(
        self, client: Any, pc_db: Any, entitlements: Entitlements
    ) -> None:
        entitlements.set_tier("free")
        response = await client.get(_base(pc_db), headers=admin_headers(pc_db))
        assert response.status_code == 402
        body = await response.get_json()
        assert body["error"]["code"] == "FEATURE_NOT_ENABLED"

    async def test_professional_and_enterprise_are_allowed(
        self, client: Any, pc_db: Any, entitlements: Entitlements
    ) -> None:
        for tier in ("professional", "enterprise"):
            entitlements.set_tier(tier)
            assert (await client.get(_base(pc_db), headers=admin_headers(pc_db))).status_code == 200

    async def test_the_gate_runs_after_the_authz_check(
        self, client: Any, pc_db: Any, entitlements: Entitlements
    ) -> None:
        """An outsider learns nothing about whether the feature is on for someone else's tenant."""
        entitlements.flag_off("waddles.integrations.provider_connections")
        response = await client.get(
            _base(pc_db, pc_db.seed.other_community_id), headers=admin_headers(pc_db)
        )
        assert response.status_code == 403


class TestCrudOverHttp:
    async def test_full_lifecycle_and_exact_dto_shape(
        self, client: Any, pc_db: Any, bot_tokens: Any, fake_twitch: FakeTwitch
    ) -> None:
        created = await _create(client, pc_db, bot_tokens)
        assert created.status_code == 201
        body = await created.get_json()
        assert set(body) == {"success", "connection"}
        conn = body["connection"]
        assert (
            set(conn) == ADMIN_FIELDS
        )  # an extra field fails loudly (security.md Output Validation)
        assert conn["provider"] == "twitch" and conn["resourceType"] == "twitch_channel"
        assert (conn["status"], conn["enabled"], conn["access"], conn["usable"]) == (
            "active",
            True,
            "approved",
            True,
        )
        assert conn["shared"] is False and conn["hasRefreshToken"] is True
        assert conn["metadata"]["channel_login"] == CHANNEL_LOGIN
        assert conn["metadata"]["bot_login"] == BOT.login
        assert "client_id" not in conn["metadata"]
        cid = conn["id"]

        listed = await (await client.get(_base(pc_db), headers=admin_headers(pc_db))).get_json()
        assert [c["id"] for c in listed["connections"]] == [cid]
        assert set(listed["connections"][0]) == ADMIN_FIELDS
        got = await (
            await client.get(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db))
        ).get_json()
        assert got["connection"] == conn

        off = await client.patch(
            f"{_base(pc_db)}/{cid}",
            headers=admin_headers(pc_db),
            data=json.dumps({"enabled": False}),
        )
        assert off.status_code == 200
        assert (await off.get_json())["connection"]["enabled"] is False
        moved = await client.patch(
            f"{_base(pc_db)}/{cid}",
            headers=admin_headers(pc_db),
            data=json.dumps({"metadata": {"channel_login": OTHER_CHANNEL_LOGIN}}),
        )
        assert (await moved.get_json())["connection"]["metadata"][
            "channel_login"
        ] == OTHER_CHANNEL_LOGIN
        new_access, new_refresh = fake_twitch.mint(account=BOT)
        rotated = await client.patch(
            f"{_base(pc_db)}/{cid}",
            headers=admin_headers(pc_db),
            data=json.dumps(
                {"credentials": {"access_token": new_access, "refresh_token": new_refresh}}
            ),
        )
        assert rotated.status_code == 200

        deleted = await client.delete(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db))
        assert deleted.status_code == 200
        assert await deleted.get_json() == {"success": True, "purged": True}
        gone = await client.get(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db))
        assert gone.status_code == 404
        assert (await gone.get_json())["error"]["code"] == "NOT_FOUND"

    async def test_no_response_ever_contains_a_token_or_ciphertext(
        self, client: Any, pc_db: Any, bot_tokens: Any, fake_twitch: FakeTwitch
    ) -> None:
        created = await _create(client, pc_db, bot_tokens)
        cid = (await created.get_json())["connection"]["id"]
        new_access, new_refresh = fake_twitch.mint(account=BOT)
        responses = [
            created,
            await client.get(_base(pc_db), headers=admin_headers(pc_db)),
            await client.get(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db)),
            await client.patch(
                f"{_base(pc_db)}/{cid}",
                headers=admin_headers(pc_db),
                data=json.dumps(
                    {"credentials": {"access_token": new_access, "refresh_token": new_refresh}}
                ),
            ),
            await client.patch(
                f"{_base(pc_db)}/{cid}",
                headers=admin_headers(pc_db),
                data=json.dumps({"enabled": False}),
            ),
        ]
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        needles = [*bot_tokens, new_access, new_refresh, row["access_token"], row["refresh_token"]]
        assert all(needles)
        for response in responses:
            text = await response.get_data(as_text=True)
            assert text  # the scan below must examine real output
            for needle in needles:
                assert needle not in text

    async def test_the_request_secret_is_not_echoed_in_validation_errors(
        self, client: Any, pc_db: Any, bot_tokens: Any
    ) -> None:
        response = await client.post(
            _base(pc_db),
            headers=admin_headers(pc_db),
            data=json.dumps(
                {
                    "provider": "twitch",
                    "credentials": {"access_token": "SECRET-IN-BAD-REQUEST", "oops": 1},
                    "metadata": {"channel_login": CHANNEL_LOGIN},
                }
            ),
        )
        assert response.status_code == 400
        assert "SECRET-IN-BAD-REQUEST" not in await response.get_data(as_text=True)

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ({"provider": "twitch"}, 400),  # credentials missing
            ({"provider": "twitch", "credentials": {}}, 400),
            ({"provider": "twitch", "credentials": {"access_token": "t"}, "extra": 1}, 400),
            (
                {
                    "provider": "twitch",
                    "credentials": {"access_token": "t"},
                    "metadata": {"channel_login": 5},
                },
                400,
            ),
            ({"provider": "twitch", "credentials": {"access_token": "t"}, "metadata": {}}, 400),
            ({"provider": "slack", "credentials": {"access_token": "t"}, "metadata": {}}, 400),
        ],
    )
    async def test_malformed_requests_are_400_and_store_nothing(
        self, client: Any, pc_db: Any, payload: dict[str, Any], expected: int
    ) -> None:
        response = await client.post(
            _base(pc_db), headers=admin_headers(pc_db), data=json.dumps(payload)
        )
        assert response.status_code == expected
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_provider_errors_map_to_clear_statuses(
        self, client: Any, pc_db: Any, bot_tokens: Any, fake_twitch: FakeTwitch
    ) -> None:
        rejected = await client.post(
            _base(pc_db),
            headers=admin_headers(pc_db),
            data=_body(("not-a-token", None)),
        )
        assert rejected.status_code == 422
        assert (await rejected.get_json())["error"]["code"] == "CREDENTIALS_REJECTED"

        missing_scope_token, _ = fake_twitch.mint(account=BOT, scopes=["user:read:email"])
        scope = await client.post(
            _base(pc_db), headers=admin_headers(pc_db), data=_body((missing_scope_token, None))
        )
        assert (scope.status_code, (await scope.get_json())["error"]["code"]) == (
            422,
            "INSUFFICIENT_SCOPES",
        )

        no_channel = await client.post(
            _base(pc_db),
            headers=admin_headers(pc_db),
            data=_body(bot_tokens, metadata={"channel_login": "nosuchchannel"}),
        )
        assert (no_channel.status_code, (await no_channel.get_json())["error"]["code"]) == (
            422,
            "RESOURCE_NOT_FOUND",
        )

        fake_twitch.down = True
        down = await _create(client, pc_db, bot_tokens)
        assert (down.status_code, (await down.get_json())["error"]["code"]) == (
            502,
            "PROVIDER_UNAVAILABLE",
        )
        fake_twitch.down = False
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_duplicate_resource_is_409(
        self, client: Any, pc_db: Any, bot_tokens: Any, fake_twitch: FakeTwitch
    ) -> None:
        assert (await _create(client, pc_db, bot_tokens)).status_code == 201
        other = fake_twitch.mint(account=BOT)
        again = await _create(client, pc_db, other)
        assert (again.status_code, (await again.get_json())["error"]["code"]) == (409, "CONFLICT")

    async def test_unknown_ids_are_404_for_get_patch_delete(self, client: Any, pc_db: Any) -> None:
        headers = admin_headers(pc_db)
        assert (await client.get(f"{_base(pc_db)}/999", headers=headers)).status_code == 404
        assert (
            await client.patch(
                f"{_base(pc_db)}/999", headers=headers, data=json.dumps({"enabled": True})
            )
        ).status_code == 404
        assert (await client.delete(f"{_base(pc_db)}/999", headers=headers)).status_code == 404

    async def test_empty_patch_is_400(self, client: Any, pc_db: Any, bot_tokens: Any) -> None:
        cid = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        response = await client.patch(
            f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db), data="{}"
        )
        assert response.status_code == 400

    async def test_a_shared_link_reports_shared_and_refuses_changes(
        self, client: Any, pc_db: Any, bot_tokens: Any, fake_twitch: FakeTwitch
    ) -> None:
        from tests.provider_connections.support import grant_to

        cid = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        await grant_to(pc_db, community_id=pc_db.seed.second_community_id, connection_id=cid)
        got = await (
            await client.get(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db))
        ).get_json()
        assert got["connection"]["shared"] is True
        new_access, _ = fake_twitch.mint(account=BOT)
        response = await client.patch(
            f"{_base(pc_db)}/{cid}",
            headers=admin_headers(pc_db),
            data=json.dumps({"credentials": {"access_token": new_access}}),
        )
        assert response.status_code == 409
        deleted = await client.delete(f"{_base(pc_db)}/{cid}", headers=admin_headers(pc_db))
        assert (await deleted.get_json()) == {"success": True, "purged": False}

    async def test_cross_community_ids_are_404_not_403(
        self, client: Any, pc_db: Any, bot_tokens: Any
    ) -> None:
        cid = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        sibling = _base(pc_db, pc_db.seed.second_community_id)
        headers = admin_headers(pc_db)
        assert (await client.get(f"{sibling}/{cid}", headers=headers)).status_code == 404
        assert (await client.delete(f"{sibling}/{cid}", headers=headers)).status_code == 404
        assert len(await raw_rows(pc_db, "platform_connections")) == 1

    async def test_vault_failure_is_a_503_that_never_names_the_key_value(
        self, client: Any, pc_db: Any, bot_tokens: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY")
        response = await _create(client, pc_db, bot_tokens)
        assert response.status_code == 503
        body = await response.get_json()
        assert body["error"]["code"] == "CONNECTIONS_UNAVAILABLE"
        assert "CREDENTIAL_ENCRYPTION_KEY" not in json.dumps(body)  # generic text for 5xx

    async def test_the_default_service_is_built_lazily_from_install_dal(
        self, pc_app: Any, client: Any, pc_db: Any
    ) -> None:
        from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY

        del pc_app.config[SERVICE_CONFIG_KEY]
        response = await client.get("/api/v1/connection-providers", headers=admin_headers(pc_db))
        assert response.status_code == 200
        assert SERVICE_CONFIG_KEY in pc_app.config


def _service_token(
    issuer: ServiceJwtIssuer,
    scope: str = "connections:resolve",
    service_id: str = SYSTEM_SERVICE_ID,
) -> str:
    return issuer.issue(service_id, scope)


def _svc(issuer: ServiceJwtIssuer, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {_service_token(issuer, **kwargs)}"}


INTERNAL = "/api/v1/internal"


class TestInternalApi:
    async def test_list_and_resolve_for_the_data_plane(
        self, client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        cid = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        listed = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch&capability=chat_ingest",
            headers=_svc(service_issuer),
        )
        assert listed.status_code == 200
        body = await listed.get_json()
        assert set(body) == {"success", "items", "nextAfter"}
        (item,) = body["items"]
        assert set(item) == {
            "grantId",
            "connectionId",
            "communityId",
            "tenantId",
            "provider",
            "resourceType",
            "resourceId",
            "capabilities",
            "expiresAt",
            "updatedAt",
            "metadata",
        }
        assert (item["connectionId"], item["communityId"], item["resourceId"]) == (
            cid,
            pc_db.seed.community_id,
            CHANNEL_ID,
        )
        text = await listed.get_data(as_text=True)
        assert bot_tokens[0] not in text and bot_tokens[1] not in text
        assert listed.headers["Cache-Control"] == "no-store"

        resolved = await client.post(
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/{cid}/resolve",
            headers=_svc(service_issuer),
        )
        assert resolved.status_code == 200
        out = await resolved.get_json()
        assert set(out) == {
            "success",
            "connectionId",
            "communityId",
            "provider",
            "accessToken",
            "expiresAt",
            "metadata",
        }
        assert out["accessToken"] == bot_tokens[0]
        assert bot_tokens[1] not in await resolved.get_data(
            as_text=True
        )  # refresh token never leaves
        assert (
            resolved.headers["Cache-Control"] == "no-store"
            and resolved.headers["Pragma"] == "no-cache"
        )
        assert out["metadata"]["bot_login"] == BOT.login

    async def test_resolve_refreshes_a_stale_token_for_the_caller(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
        fake_twitch: FakeTwitch,
    ) -> None:
        cid = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        clock.advance(seconds=14000)
        resolved = await client.post(
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/{cid}/resolve",
            headers=_svc(service_issuer),
        )
        assert resolved.status_code == 200 and fake_twitch.refresh_calls == 1
        assert (await resolved.get_json())["accessToken"] != bot_tokens[0]

    async def test_every_internal_route_demands_a_machine_jwt(
        self, client: Any, pc_db: Any
    ) -> None:
        urls = [
            ("get", f"{INTERNAL}/provider-connections?provider=twitch"),
            ("post", f"{INTERNAL}/communities/1/provider-connections/1/resolve"),
        ]
        for method, url in urls:
            assert (await getattr(client, method)(url)).status_code == 401
            bad = await getattr(client, method)(url, headers={"Authorization": "Bearer not-a-jwt"})
            assert bad.status_code == 401
            # a user (HS256) JWT is not a machine JWT
            user = {
                "Authorization": f"Bearer {make_user_token(user_id=1, scope='connections:resolve')}"
            }
            assert (await getattr(client, method)(url, headers=user)).status_code == 401
            # the old shared-key mechanism is not accepted
            key = await getattr(client, method)(url, headers={"X-Service-Key": "anything"})
            assert key.status_code == 401

    async def test_the_scope_is_exact(
        self, client: Any, pc_db: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        response = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch",
            headers=_svc(service_issuer, scope="other:scope"),
        )
        assert response.status_code == 401

    async def test_only_the_operator_plane_tenant_may_call(self, client: Any, pc_db: Any) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from flask_core.service_jwt import SigningKey

        private = Ed25519PrivateKey.generate()
        issuer = ServiceJwtIssuer(
            keys={"k1": SigningKey("k1", private, private.public_key())},
            active_kid="k1",
            identities={
                SYSTEM_SERVICE_ID: ServiceIdentity(
                    SYSTEM_SERVICE_ID,
                    "waddlebot",
                    "svc-ingest",
                    frozenset({"connections:resolve"}),
                    tenant="acme-corp",
                )
            },
            audience=SERVICE_AUDIENCE,
        )
        client.app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
        response = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch", headers=_svc(issuer)
        )
        assert (
            response.status_code == 403
        )  # a single-tenant service identity cannot enumerate tenants

    async def test_no_verifier_configured_fails_closed(
        self, client: Any, pc_app: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        pc_app.config["SERVICE_JWT_VERIFIER"] = None
        response = await client.get(
            f"{INTERNAL}/provider-connections?provider=twitch", headers=_svc(service_issuer)
        )
        assert response.status_code == 503

    async def test_input_validation_on_the_list(
        self, client: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        h = _svc(service_issuer)
        assert (await client.get(f"{INTERNAL}/provider-connections", headers=h)).status_code == 400
        for query in (
            "provider=slack",
            "provider=twitch&capability=telepathy",
            "provider=twitch&after=x",
            "provider=twitch&after=-1",
            "provider=twitch&limit=0",
            "provider=twitch&limit=5000",
        ):
            assert (
                await client.get(f"{INTERNAL}/provider-connections?{query}", headers=h)
            ).status_code == 400, query

    async def test_pagination_cursor_round_trip(
        self, client: Any, pc_db: Any, fake_twitch: FakeTwitch, service_issuer: ServiceJwtIssuer
    ) -> None:
        for n in range(3):
            login = f"chan{n}x"
            fake_twitch.channels[login] = f"90{n}"
            tokens = fake_twitch.mint(account=BOT)
            assert (
                await _create(client, pc_db, tokens, metadata={"channel_login": login})
            ).status_code == 201
        first = await (
            await client.get(
                f"{INTERNAL}/provider-connections?provider=twitch&limit=2",
                headers=_svc(service_issuer),
            )
        ).get_json()
        assert len(first["items"]) == 2 and first["nextAfter"] is not None
        second = await (
            await client.get(
                f"{INTERNAL}/provider-connections?provider=twitch&limit=2&after={first['nextAfter']}",
                headers=_svc(service_issuer),
            )
        ).get_json()
        assert len(second["items"]) == 1 and second["nextAfter"] is None

    async def test_resolve_refusals_carry_a_machine_readable_reason(
        self, client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        created = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]
        cid = created["id"]
        url = f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/{cid}/resolve"
        await client.patch(
            f"{_base(pc_db)}/{cid}",
            headers=admin_headers(pc_db),
            data=json.dumps({"enabled": False}),
        )
        refused = await client.post(url, headers=_svc(service_issuer))
        assert refused.status_code == 409
        err = (await refused.get_json())["error"]
        assert (err["code"], err["details"]) == ("CONNECTION_NOT_USABLE", {"reason": "disabled"})
        missing = await client.post(
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/777/resolve",
            headers=_svc(service_issuer),
        )
        assert missing.status_code == 404

    async def test_provider_outage_during_refresh_is_a_retryable_502(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
        fake_twitch: FakeTwitch,
    ) -> None:
        cid = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        clock.advance(seconds=14000)
        fake_twitch.down = True
        response = await client.post(
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/{cid}/resolve",
            headers=_svc(service_issuer),
        )
        assert response.status_code == 502
        assert (await response.get_json())["error"]["code"] == "PROVIDER_UNAVAILABLE"
        row = (await raw_rows(pc_db, "platform_connections"))[0]
        assert row["status"] == "active"  # an outage never kills the connection

    async def test_a_tampered_row_is_a_500_with_generic_text(
        self, client: Any, pc_db: Any, bot_tokens: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        cid = (await (await _create(client, pc_db, bot_tokens)).get_json())["connection"]["id"]
        await set_column(
            pc_db, "platform_connections", cid, access_token="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        response = await client.post(
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/{cid}/resolve",
            headers=_svc(service_issuer),
        )
        assert response.status_code == 500
        text = await response.get_data(as_text=True)
        assert "AAAA" not in text and "authentication" not in text  # no internals leaked


class TestHttpDoesNotLeakIntoLogs:
    async def test_http_flow_logs_no_secret(
        self,
        client: Any,
        pc_db: Any,
        bot_tokens: Any,
        service_issuer: ServiceJwtIssuer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        for name in (
            "services.provider_connections",
            "blueprints.v1.provider_connections",
            "httpx",
        ):
            caplog.set_level(logging.DEBUG, logger=name)
        created = await _create(client, pc_db, bot_tokens)
        cid = (await created.get_json())["connection"]["id"]
        resolved = await client.post(
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/{cid}/resolve",
            headers=_svc(service_issuer),
        )
        await client.get(_base(pc_db), headers=admin_headers(pc_db))
        await _create(client, pc_db, ("bogus-token", None))
        token = (await resolved.get_json())["accessToken"]
        records = [
            r
            for r in caplog.records
            if r.name.startswith(
                ("services.provider_connections", "blueprints.v1.provider_connections", "httpx")
            )
        ]
        assert len(records) > 3
        text = "\n".join(r.getMessage() for r in records)
        for secret in (*bot_tokens, token, "bogus-token", _service_token(service_issuer)):
            assert secret not in text
        assert BOT.login not in text and CHANNEL_LOGIN not in text
