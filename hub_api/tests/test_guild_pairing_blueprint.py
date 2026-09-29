"""HTTP-layer tests for `blueprints/v1/guild_pairing.py` (tenant-admin + guild-authority routes).

Complements the real-Postgres service-layer tests (`test_guild_pairing_
service.py`/`test_guild_pairing_authority_service.py`, which exercise the
DB triggers/partial-unique-indexes for real) with what only a live Quart
request/response cycle can prove: scope enforcement, tenant isolation at
the route layer, the feature-flag-off 404, `@validate_response`'s exact
field set (security.md Output Validation -- an accidentally-added field
would pass every service-layer test and only show up here), the 409's
non-leaky body over the wire, and the `GuildAuthorityVerifier`-unwired 503.

Uses the sqlite `install_dal`/`bundle_install_db` fixtures (`tests/
conftest.py::_create_bundle_install_tables`, extended with `guild_tenant_
pairings`/`community_channel_bindings`/`managed_roles` mirrors including
the two partial unique indexes) -- fast, and sufficient for every
assertion here since the exclusivity/trigger *mechanics* are already
proven against real Postgres in the service-layer test files.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1 import guild_pairing as guild_pairing_module
from blueprints.v1.guild_pairing import BLUEPRINTS
from services.guild_pairing_authority_service import GuildAuthorityVerifier
from tests.conftest import TENANT_SLUG, make_user_token


class _AllowVerifier:
    """Always grants guild-authority -- the default for tests that don't care."""

    async def verify(self, *, platform: str, guild_id: str, hub_user_id: int) -> bool:
        return True


class _DenyVerifier:
    """Always denies guild-authority."""

    async def verify(self, *, platform: str, guild_id: str, hub_user_id: int) -> bool:
        return False


@pytest.fixture
def _feature_enabled_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the `waddles.guild-pairing` flag ON for every test except the flag-off ones."""

    async def _fake_feature_enabled(flag_key: str, *, tenant: str, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(guild_pairing_module, "feature_enabled", _fake_feature_enabled)


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any, _feature_enabled_on: None) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    quart_app.config["guild_authority_verifier"] = _AllowVerifier()
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


async def _seed_pairing(
    install_dal: Any, *, tenant_id: int, guild_id: str = "111111", status: str = "active"
) -> str:
    return str(
        await install_dal.guild_tenant_pairings.async_insert(
            platform="discord", guild_id=guild_id, tenant_id=tenant_id, status=status
        )
    )


async def _seed_community(install_dal: Any, *, tenant_id: int, name: str = "acme-community") -> int:
    return int(await install_dal.communities.async_insert(tenant_id=tenant_id, name=name))


def _admin_token(tenant: str = TENANT_SLUG) -> str:
    return make_user_token(user_id=1, scope="tenant:admin tenant:read", tenant=tenant)


def _viewer_token(tenant: str = TENANT_SLUG) -> str:
    return make_user_token(user_id=1, scope="tenant:read", tenant=tenant)


def _guild_authority_token() -> str:
    return make_user_token(
        user_id=1, scope="guild.authority:read guild.authority:write", tenant=TENANT_SLUG
    )


def _guild_authority_read_only_token() -> str:
    return make_user_token(user_id=1, scope="guild.authority:read", tenant=TENANT_SLUG)


# --------------------------------------------------------------------------
# Feature flag
# --------------------------------------------------------------------------


async def test_flag_off_is_404(
    bundle_install_db: Any, install_dal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_feature_disabled(flag_key: str, *, tenant: str, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(guild_pairing_module, "feature_enabled", _fake_feature_disabled)

    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    response = await quart_app.test_client().get(
        "/api/v1/tenant/guild-pairings",
        headers={"Authorization": f"Bearer {_admin_token()}"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Scope / tenant authz
# --------------------------------------------------------------------------


async def test_list_pairings_missing_scope_is_403(app: Quart) -> None:
    response = await app.test_client().get(
        "/api/v1/tenant/guild-pairings",
        headers={
            "Authorization": f"Bearer {make_user_token(user_id=1, scope='', tenant=TENANT_SLUG)}"
        },
    )
    assert response.status_code == 403


async def test_create_binding_requires_admin_not_viewer_scope(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    tenant_id = 1
    community_id = await _seed_community(install_dal, tenant_id=tenant_id)
    pairing_id = await _seed_pairing(install_dal, tenant_id=tenant_id)
    response = await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{pairing_id}/bindings",
        headers={"Authorization": f"Bearer {_viewer_token()}"},
        json={"communityId": community_id},
    )
    assert response.status_code == 403


async def test_binding_on_another_tenants_pairing_is_404(app: Quart) -> None:
    """Cross-tenant: the pairing exists, just not for the caller's own tenant."""
    dal = app.config["dal"]
    other_tenant_id = int(dal.tenants.insert(slug="other-corp", is_active=True))
    dal.commit()
    install_dal = app.config["install_dal"]
    other_pairing_id = await _seed_pairing(install_dal, tenant_id=other_tenant_id)
    community_id = await _seed_community(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{other_pairing_id}/bindings",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_id},
    )
    assert response.status_code == 404


async def test_binding_without_active_pairing_is_422(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _seed_community(install_dal, tenant_id=1)
    pending_pairing_id = await _seed_pairing(install_dal, tenant_id=1, status="pending")
    response = await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{pending_pairing_id}/bindings",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_id},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Response schema -- exact field set (security.md Output Validation)
# --------------------------------------------------------------------------


async def test_list_pairings_response_has_exact_field_set(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    await _seed_pairing(install_dal, tenant_id=1)
    response = await app.test_client().get(
        "/api/v1/tenant/guild-pairings", headers={"Authorization": f"Bearer {_admin_token()}"}
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body.keys()) == {"success", "pairings"}
    assert set(body["pairings"][0].keys()) == {
        "id",
        "platform",
        "guildId",
        "status",
        "consentAt",
        "lastVerifiedAt",
        "revokedAt",
        "revokedBy",
        "createdAt",
    }


async def test_create_binding_response_has_exact_field_set(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _seed_community(install_dal, tenant_id=1)
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{pairing_id}/bindings",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_id},
    )
    assert response.status_code == 201
    body = await response.get_json()
    assert set(body.keys()) == {"success", "binding"}
    assert set(body["binding"].keys()) == {
        "id",
        "platform",
        "guildId",
        "channelId",
        "communityId",
        "pairingId",
        "status",
        "createdAt",
    }


async def test_role_registration_response_has_exact_field_set(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _seed_community(install_dal, tenant_id=1)
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{pairing_id}/roles",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_id, "roleId": "555555", "registeredVia": "created"},
    )
    assert response.status_code == 201
    body = await response.get_json()
    assert set(body.keys()) == {"success", "managedRole"}
    assert set(body["managedRole"].keys()) == {
        "id",
        "platform",
        "guildId",
        "roleId",
        "owningCommunityId",
        "registeredVia",
        "status",
        "approvalStatus",
        "approvedByUserId",
        "createdAt",
    }
    assert body["managedRole"]["approvalStatus"] == "approved"


async def test_adopted_role_registration_is_pending(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _seed_community(install_dal, tenant_id=1)
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{pairing_id}/roles",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_id, "roleId": "556666", "registeredVia": "adopted"},
    )
    assert response.status_code == 201
    body = await response.get_json()
    assert body["managedRole"]["approvalStatus"] == "pending"
    assert body["managedRole"]["status"] == "pending_approval"
    assert body["managedRole"]["approvedByUserId"] is None


# --------------------------------------------------------------------------
# 409 non-leak
# --------------------------------------------------------------------------


async def test_guild_default_binding_conflict_does_not_leak_other_tenant(app: Quart) -> None:
    dal = app.config["dal"]
    other_tenant_id = int(dal.tenants.insert(slug="other-corp-2", is_active=True))
    dal.commit()
    install_dal = app.config["install_dal"]
    guild_id = "999999"
    community_a = await _seed_community(install_dal, tenant_id=1, name="community-a")
    community_b = await _seed_community(install_dal, tenant_id=other_tenant_id, name="community-b")
    pairing_a = await _seed_pairing(install_dal, tenant_id=1, guild_id=guild_id)
    pairing_b = await _seed_pairing(install_dal, tenant_id=other_tenant_id, guild_id=guild_id)

    client = app.test_client()
    first = await client.post(
        f"/api/v1/tenant/guild-pairings/{pairing_a}/bindings",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_a},
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/tenant/guild-pairings/{pairing_b}/bindings",
        headers={"Authorization": f"Bearer {_admin_token(tenant='other-corp-2')}"},
        json={"communityId": community_b},
    )
    assert second.status_code == 409
    body = await second.get_json()
    # Non-leaky: exactly the static, generic {success, error:{code,message,
    # timestamp}} shape -- no tenant/community-identifying field of any kind.
    assert set(body.keys()) == {"success", "error"}
    assert set(body["error"].keys()) == {"code", "message", "timestamp"}
    assert body["error"]["code"] == "BINDING_CONFLICT"
    body_text = str(body)
    assert TENANT_SLUG not in body_text
    assert "other-corp-2" not in body_text
    assert "community-a" not in body_text
    assert "community-b" not in body_text


# --------------------------------------------------------------------------
# Guild-authority routes
# --------------------------------------------------------------------------


async def test_revoke_pairing_verifier_unwired_is_503(
    bundle_install_db: Any, install_dal: Any, _feature_enabled_on: None
) -> None:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    # Deliberately no "guild_authority_verifier" key at all.
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    response = await quart_app.test_client().post(
        f"/api/v1/guild-authority/{pairing_id}/revoke",
        headers={"Authorization": f"Bearer {_guild_authority_token()}"},
    )
    assert response.status_code == 503


async def test_revoke_pairing_denied_without_guild_authority_is_403(
    bundle_install_db: Any, install_dal: Any, _feature_enabled_on: None
) -> None:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    quart_app.config["guild_authority_verifier"] = _DenyVerifier()
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    response = await quart_app.test_client().post(
        f"/api/v1/guild-authority/{pairing_id}/revoke",
        headers={"Authorization": f"Bearer {_guild_authority_token()}"},
    )
    assert response.status_code == 403


async def test_revoke_pairing_requires_guild_authority_scope(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/guild-authority/{pairing_id}/revoke",
        headers={
            "Authorization": f"Bearer {make_user_token(user_id=1, scope='', tenant=TENANT_SLUG)}"
        },
    )
    assert response.status_code == 403


async def test_revoke_pairing_succeeds_with_guild_authority(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    response = await app.test_client().post(
        f"/api/v1/guild-authority/{pairing_id}/revoke",
        headers={"Authorization": f"Bearer {_guild_authority_token()}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body.keys()) == {"success", "pairing"}
    assert body["pairing"]["status"] == "revoked"


async def test_approve_adopted_role_write_scope_required(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _seed_community(install_dal, tenant_id=1)
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    created = await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{pairing_id}/roles",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_id, "roleId": "600000", "registeredVia": "adopted"},
    )
    managed_role_id = (await created.get_json())["managedRole"]["id"]
    response = await app.test_client().post(
        f"/api/v1/guild-authority/managed-roles/{managed_role_id}/approve",
        headers={"Authorization": f"Bearer {_guild_authority_read_only_token()}"},
    )
    assert response.status_code == 403


async def test_approve_adopted_role_succeeds(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _seed_community(install_dal, tenant_id=1)
    pairing_id = await _seed_pairing(install_dal, tenant_id=1)
    created = await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{pairing_id}/roles",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_id, "roleId": "600001", "registeredVia": "adopted"},
    )
    managed_role_id = (await created.get_json())["managedRole"]["id"]
    response = await app.test_client().post(
        f"/api/v1/guild-authority/managed-roles/{managed_role_id}/approve",
        headers={"Authorization": f"Bearer {_guild_authority_token()}"},
    )
    assert response.status_code == 201
    body = await response.get_json()
    assert body["managedRole"]["approvalStatus"] == "approved"
    assert body["managedRole"]["approvedByUserId"] == 1


async def test_guild_overview_no_member_data_field(app: Quart) -> None:
    install_dal = app.config["install_dal"]
    community_id = await _seed_community(install_dal, tenant_id=1)
    guild_id = "700000"
    pairing_id = await _seed_pairing(install_dal, tenant_id=1, guild_id=guild_id)
    await app.test_client().post(
        f"/api/v1/tenant/guild-pairings/{pairing_id}/bindings",
        headers={"Authorization": f"Bearer {_admin_token()}"},
        json={"communityId": community_id},
    )
    response = await app.test_client().get(
        f"/api/v1/guild-authority/discord/{guild_id}/overview",
        headers={"Authorization": f"Bearer {_guild_authority_read_only_token()}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body.keys()) == {"success", "entries"}
    entry_keys = set(body["entries"][0].keys())
    assert entry_keys == {"pairingId", "tenantId", "status", "boundCommunityIds", "ownedRoleIds"}
    assert "member" not in " ".join(entry_keys).lower()


# Sanity: `GuildAuthorityVerifier` is `@runtime_checkable` -- confirm the
# test doubles above actually satisfy the Protocol shape the blueprint injects.
def test_fake_verifiers_satisfy_the_protocol() -> None:
    assert isinstance(_AllowVerifier(), GuildAuthorityVerifier)
    assert isinstance(_DenyVerifier(), GuildAuthorityVerifier)
