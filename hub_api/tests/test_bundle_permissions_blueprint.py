"""Blueprint tests for the permission consent flow -- scope checks at all 3 tiers + revoke."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_permissions import BLUEPRINTS
from tests.conftest import make_token, make_user_token, make_user_token_with_roles

_APP_ID = "waddles.socials.music.default"
_VERSION = "3.0.0"
_MANIFEST = {
    "schema_version": 2,
    "app_id": _APP_ID,
    "name": "Music Station",
    "version": _VERSION,
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {
        "process": {
            "entry": "x:y",
            "consumes": [{"platform": "twitch", "event_types": ["chat.message"]}],
        }
    },
    "permissions": [
        {"id": "storage.kv", "justification": "Stores state."},
        {"id": "ai.generate", "justification": "Generates a shoutout line."},
    ],
}


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    dal = bundle_install_db.dal
    dal.define_table("community_roles", Field("name"), Field("base_claims", "json"), migrate=True)
    dal.define_table(
        "community_members",
        Field("community_id", "integer"),
        Field("user_id"),
        Field("is_active", "boolean", default=True),
        Field("claims_cache", "json"),
        Field("role"),
        Field("community_role_id", "integer"),
        migrate=True,
    )
    dal.define_table(
        "tenant_admins", Field("user_id", "integer"), Field("tenant_id", "integer"), migrate=True
    )
    dal.define_table("communities", Field("tenant_id", "integer"), Field("name"), migrate=False)
    dal(dal.community_members.id == 0).count()
    dal.commit()

    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id=_APP_ID,
        version=_VERSION,
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=_MANIFEST,
        created_at=now,
        updated_at=now,
    )

    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


async def _seed_community(install_dal: Any, *, tenant_id: int = 1, name: str = "acme") -> int:
    return int(await install_dal.communities.async_insert(tenant_id=tenant_id, name=name))


# ---------------------------------------------------------------------------
# GLOBAL tier
# ---------------------------------------------------------------------------


async def test_approve_requires_platform_admin(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/permissions/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"approvedPermissions": ["ai.generate"]},
    )
    assert response.status_code == 403


async def test_approve_happy_path_requires_dangerous_ack(app: Quart) -> None:
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/permissions/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"approvedPermissions": []},
    )
    assert response.status_code == 422
    body = await response.get_json()
    assert body["error"]["code"] == "incomplete_dangerous_ack"


async def test_approve_happy_path(app: Quart) -> None:
    token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/permissions/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"approvedPermissions": ["ai.generate"]},
    )
    assert response.status_code == 200

    listing = await client.get(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/permissions/approved",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listing.status_code == 200
    body = await listing.get_json()
    assert sorted(body["permissionIds"]) == ["ai.generate", "storage.kv"]
    # security.md Output Validation: DTO field set is exactly these two keys.
    assert set(body.keys()) == {"success", "permissionIds"}


# ---------------------------------------------------------------------------
# TENANT tier
# ---------------------------------------------------------------------------


async def test_restrict_requires_tenant_admin(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.put(
        f"/api/v1/apps/tenant/acme-corp/{_APP_ID}/versions/{_VERSION}/permissions/restrict",
        headers={"Authorization": f"Bearer {token}"},
        json={"restrictedPermissionIds": []},
    )
    assert response.status_code == 403


async def test_restrict_happy_path(app: Quart) -> None:
    admin_token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    await client.post(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/permissions/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"approvedPermissions": ["ai.generate"]},
    )
    tenant_token = make_token(scope="tenant:admin", user_id="1")
    response = await client.put(
        f"/api/v1/apps/tenant/acme-corp/{_APP_ID}/versions/{_VERSION}/permissions/restrict",
        headers={"Authorization": f"Bearer {tenant_token}"},
        json={"restrictedPermissionIds": ["ai.generate"]},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# COMMUNITY tier
# ---------------------------------------------------------------------------


async def test_grant_requires_community_admin(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    token = make_user_token(user_id=999, scope="")
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/grant",
        headers={"Authorization": f"Bearer {token}"},
        json={"version": _VERSION, "grantedPermissions": ["storage.kv", "ai.generate"]},
    )
    assert response.status_code == 403


async def test_grant_consent_required_when_partial(app: Quart, install_dal: Any) -> None:
    admin_token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    await client.post(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/permissions/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"approvedPermissions": ["ai.generate"]},
    )
    community_id = await _seed_community(install_dal)
    token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    response = await client.post(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/grant",
        headers={"Authorization": f"Bearer {token}"},
        json={"version": _VERSION, "grantedPermissions": ["storage.kv"]},
    )
    assert response.status_code == 422
    body = await response.get_json()
    assert body["error"]["code"] == "consent_required"


async def test_grant_then_revoke_happy_path(app: Quart, install_dal: Any) -> None:
    admin_token = make_token(scope="platform:admin", user_id="1")
    client = app.test_client()
    await client.post(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/permissions/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"approvedPermissions": ["ai.generate"]},
    )
    community_id = await _seed_community(install_dal)
    token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    grant_response = await client.post(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/grant",
        headers={"Authorization": f"Bearer {token}"},
        json={"version": _VERSION, "grantedPermissions": ["storage.kv", "ai.generate"]},
    )
    assert grant_response.status_code == 201
    grant_body = await grant_response.get_json()
    assert grant_body["grantVersion"] == 1
    assert set(grant_body.keys()) == {"success", "grantVersion"}

    revoke_response = await client.delete(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/ai.generate"
        f"?version={_VERSION}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert revoke_response.status_code == 200
    revoke_body = await revoke_response.get_json()
    assert revoke_body["grantVersion"] == 2

    listing = await client.get(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/granted",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = await listing.get_json()
    assert body["permissionIds"] == ["storage.kv"]


async def test_revoke_requires_community_admin(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    token = make_user_token(user_id=999, scope="")
    client = app.test_client()
    response = await client.delete(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/ai.generate"
        f"?version={_VERSION}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
