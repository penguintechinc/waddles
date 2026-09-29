"""Blueprint tests for the COMMUNITY tier: GET/POST/DELETE `/api/v1/apps/community/<id>/activation`.

Community-admin membership is DB-backed (`services.community_authz`), not
a flat JWT scope -- happy-path tests use the `platform-admin` ROLE-claim
bypass (`community_authz._BYPASS_ROLES`) rather than seeding a full
`community_members`/`community_roles` fixture, since that authz module's
own resolution logic already has dedicated coverage in
`test_community_authz.py`; this file only proves the gate is wired to
the right service calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_activation import BLUEPRINTS
from services.bundle_approval_service import install_version_globally
from services.tenant_app_availability_service import set_available
from tests.conftest import make_user_token, make_user_token_with_roles

_MANIFEST = {
    "schema_version": 2,
    "app_id": "waddles.socials.music.default",
    "name": "Music Station",
    "version": "3.0.1",
    "feature": "waddles.socials.music",
    "module": "socials",
    "provider": "builtin",
    "language": "python",
    "artifact": "source",
    "stages": {"process": {"entry": "x:y", "consumes": []}},
}


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    dal = bundle_install_db.dal
    # Minimal mirrors of the tables `services.community_authz.
    # resolve_community_membership_scoped()` reads for a NON-bypass caller
    # (the "no membership row" 403 case below) -- left empty otherwise.
    dal.define_table(
        "community_roles",
        Field("name"),
        Field("base_claims", "json"),
        migrate=True,
    )
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
        "tenant_admins",
        Field("user_id", "integer"),
        Field("tenant_id", "integer"),
        migrate=True,
    )
    # `community_authz.community_belongs_to_tenant()` reads `communities` through the PYDAL
    # `dal`, not `install_dal` (a separate SQLAlchemy connector onto the same sqlite file) --
    # `_seed_community()` below writes through `install_dal.communities.async_insert()`.
    # `migrate=False` -- the `install_dal` fixture (a dependency of this one) already
    # physically created `communities` via `_create_bundle_install_tables()`'s own
    # SQLAlchemy Core DDL; this pydal-side definition only needs to map onto that EXISTING
    # table so `dal.communities.id`/`.tenant_id` resolve, never re-CREATE it (`migrate=True`
    # here would raise `table "communities" already exists`).
    dal.define_table(
        "communities",
        Field("tenant_id", "integer"),
        Field("name"),
        migrate=False,
    )
    dal(dal.community_members.id == 0).count()  # force physical CREATE TABLE, see bundle_install_db
    dal.commit()

    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        artifact_digest="sha256:" + "a" * 64,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id="waddles.socials.music.default",
        version="3.0.1",
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=_MANIFEST,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    await set_available(
        install_dal, tenant_id=1, app_id="waddles.socials.music.default", updated_by=1
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


async def test_list_requires_membership(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    token = make_user_token(user_id=999, scope="")
    client = app.test_client()
    response = await client.get(
        f"/api/v1/apps/community/{community_id}/activation",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_activate_requires_admin(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    token = make_user_token(user_id=999, scope="")
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/community/{community_id}/activation",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    assert response.status_code == 403


async def test_activate_happy_path(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/community/{community_id}/activation",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    assert response.status_code == 201
    active = (
        await install_dal(
            (install_dal.app_active_versions.community_id == community_id)
            & (install_dal.app_active_versions.app_id == "waddles.socials.music.default")
        ).select()
    ).first()
    assert active is not None


async def test_activate_without_availability_is_409(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    await install_dal(
        (install_dal.bundle_tenant_availability.tenant_id == 1)
        & (install_dal.bundle_tenant_availability.app_id == "waddles.socials.music.default")
    ).update(available=False)
    token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/community/{community_id}/activation",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    assert response.status_code == 409


async def test_activate_unknown_community_is_404(app: Quart) -> None:
    token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/community/999999/activation",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    assert response.status_code == 404


async def test_deactivate_requires_admin(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    token = make_user_token(user_id=999, scope="")
    client = app.test_client()
    response = await client.delete(
        f"/api/v1/apps/community/{community_id}/activation/waddles.socials.music.default",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_deactivate_happy_path(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    admin_token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    client = app.test_client()
    await client.post(
        f"/api/v1/apps/community/{community_id}/activation",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    response = await client.delete(
        f"/api/v1/apps/community/{community_id}/activation/waddles.socials.music.default",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    active = await install_dal(
        install_dal.app_active_versions.community_id == community_id
    ).select()
    assert not active


async def test_list_happy_path_after_activation(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    admin_token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    client = app.test_client()
    await client.post(
        f"/api/v1/apps/community/{community_id}/activation",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    response = await client.get(
        f"/api/v1/apps/community/{community_id}/activation",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["bundles"][0]["appId"] == "waddles.socials.music.default"
