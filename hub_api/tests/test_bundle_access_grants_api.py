"""Access-grant (platform.table.*) approval, consent, widening and revocation API tests."""

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
    "table_access": [
        {
            "table": "community_members",
            "columns": ["role"],
            "ops": ["read", "write"],
            "reason": "Mirror Discord role to community role",
        }
    ],
}
_ROLE_IDS = [
    "platform.table.read:community_members.role",
    "platform.table.write:community_members.role",
]
_WIDE_VERSION = "3.1.0"


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
    # A later version that WIDENS access (adds a read-only column).
    wide = {
        **_MANIFEST,
        "version": _WIDE_VERSION,
        "table_access": [
            {**_MANIFEST["table_access"][0], "columns": ["role", "is_active"], "ops": ["read"]},
        ],
    }
    await install_dal.app_version_uploads.async_insert(
        app_id=_APP_ID,
        version=_WIDE_VERSION,
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=wide,
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


ADMIN = {"scope": "platform:admin", "user_id": "1"}


async def _approve(client: Any, version: str, ids: list[str]) -> Any:
    return await client.post(
        f"/api/v1/apps/{_APP_ID}/versions/{version}/permissions/approve",
        headers={"Authorization": f"Bearer {make_token(**ADMIN)}"},
        json={"approvedPermissions": ids},
    )


async def test_access_grants_requires_platform_admin(app: Quart) -> None:
    response = await app.test_client().get(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/access-grants",
        headers={"Authorization": f"Bearer {make_token(scope='')}"},
    )
    assert response.status_code == 403


async def test_access_grants_lists_table_access_with_explicit_dto(app: Quart) -> None:
    client = app.test_client()
    response = await client.get(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/access-grants",
        headers={"Authorization": f"Bearer {make_token(**ADMIN)}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert set(body) == {"success", "requestedIds", "approvedIds", "dangerousIds", "tableAccess"}
    assert body["requestedIds"] == _ROLE_IDS
    assert body["approvedIds"] == []  # never auto-granted: nothing approved yet
    assert body["dangerousIds"] == _ROLE_IDS
    assert body["tableAccess"] == [
        {
            "table": "community_members",
            "columns": ["role"],
            "ops": ["read", "write"],
            "reason": "Mirror Discord role to community role",
        }
    ]
    assert set(body["tableAccess"][0]) == {"table", "columns", "ops", "reason"}


async def test_approval_requires_explicit_ack_of_every_table_access_id(app: Quart) -> None:
    client = app.test_client()
    partial = await _approve(client, _VERSION, [_ROLE_IDS[0]])
    assert partial.status_code == 422
    assert (await partial.get_json())["error"]["code"] == "incomplete_dangerous_ack"
    ok = await _approve(client, _VERSION, _ROLE_IDS)
    assert ok.status_code == 200
    listing = await client.get(
        f"/api/v1/apps/{_APP_ID}/versions/{_VERSION}/access-grants",
        headers={"Authorization": f"Bearer {make_token(**ADMIN)}"},
    )
    assert (await listing.get_json())["approvedIds"] == _ROLE_IDS


async def test_widened_manifest_is_unapproved_until_reapproved(app: Quart) -> None:
    client = app.test_client()
    await _approve(client, _VERSION, _ROLE_IDS)
    wide = await client.get(
        f"/api/v1/apps/{_APP_ID}/versions/{_WIDE_VERSION}/access-grants",
        headers={"Authorization": f"Bearer {make_token(**ADMIN)}"},
    )
    body = await wide.get_json()
    # The new version requests an id the old approval never covered, and the
    # approval of 3.0.0 does not carry over: grants are per (app, version, id).
    assert "platform.table.read:community_members.is_active" in body["requestedIds"]
    assert body["approvedIds"] == []


async def test_community_access_grants_requires_admin_and_version(
    app: Quart, install_dal: Any
) -> None:
    community_id = await _seed_community(install_dal)
    client = app.test_client()
    denied = await client.get(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/access-grants?version={_VERSION}",
        headers={"Authorization": f"Bearer {make_user_token(user_id=999, scope='')}"},
    )
    assert denied.status_code == 403
    admin = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    missing = await client.get(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/access-grants",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert missing.status_code == 400
    ok = await client.get(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/access-grants?version={_VERSION}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert ok.status_code == 200
    assert (await ok.get_json())["requestedIds"] == _ROLE_IDS


async def test_community_consent_grant_then_revocation_takes_effect(
    app: Quart, install_dal: Any
) -> None:
    client = app.test_client()
    await _approve(client, _VERSION, _ROLE_IDS)
    community_id = await _seed_community(install_dal)
    token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    headers = {"Authorization": f"Bearer {token}"}
    partial = await client.post(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/grant",
        headers=headers,
        json={"version": _VERSION, "grantedPermissions": [_ROLE_IDS[0]]},
    )
    assert partial.status_code == 422
    assert (await partial.get_json())["error"]["code"] == "consent_required"
    granted = await client.post(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/grant",
        headers=headers,
        json={"version": _VERSION, "grantedPermissions": _ROLE_IDS},
    )
    assert granted.status_code == 201
    revoked = await client.delete(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/{_ROLE_IDS[1]}"
        f"?version={_VERSION}",
        headers=headers,
    )
    assert revoked.status_code == 200
    listing = await client.get(
        f"/api/v1/apps/community/{community_id}/{_APP_ID}/permissions/granted", headers=headers
    )
    assert (await listing.get_json())["permissionIds"] == [_ROLE_IDS[0]]
