"""Community activation: dangerous access-grant preview + mandatory acknowledgement."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_activation import BLUEPRINTS
from services import bundle_permission_service as perm_svc
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
    "table_access": [
        {
            "table": "community_members",
            "columns": ["role"],
            "ops": ["read", "write"],
            "reason": "Mirror Discord role to community role",
        }
    ],
}
_APP = "waddles.socials.music.default"
_IDS = [
    "platform.table.read:community_members.role",
    "platform.table.write:community_members.role",
]


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
    manifest = await perm_svc.manifest_for_version(install_dal, app_id=_APP, version="3.0.1")
    await perm_svc.record_permission_requests(
        install_dal,
        app_id=_APP,
        version="3.0.1",
        declarations=manifest.permission_declarations,
        approved_by=1,
        approved_permissions=frozenset(_IDS),
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


def _admin() -> dict[str, str]:
    token = make_user_token_with_roles(user_id=1, roles=["platform-admin"])
    return {"Authorization": f"Bearer {token}"}


async def test_preview_requires_community_admin(app: Quart, install_dal: Any) -> None:
    community_id = await _seed_community(install_dal)
    response = await app.test_client().get(
        f"/api/v1/apps/community/{community_id}/activation/{_APP}/access-grants",
        headers={"Authorization": f"Bearer {make_user_token(user_id=999, scope='')}"},
    )
    assert response.status_code == 403


async def test_preview_shows_the_approved_access_for_the_version_to_activate(
    app: Quart, install_dal: Any
) -> None:
    community_id = await _seed_community(install_dal)
    response = await app.test_client().get(
        f"/api/v1/apps/community/{community_id}/activation/{_APP}/access-grants",
        headers=_admin(),
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["approvedIds"] == _IDS
    assert body["tableAccess"][0]["reason"] == "Mirror Discord role to community role"


async def test_activation_without_ack_is_refused_and_with_ack_succeeds(
    app: Quart, install_dal: Any
) -> None:
    community_id = await _seed_community(install_dal)
    client = app.test_client()
    url = f"/api/v1/apps/community/{community_id}/activation"
    for ack in ([], [_IDS[0]]):
        refused = await client.post(
            url, headers=_admin(), json={"appId": _APP, "acknowledgedAccessGrants": ack}
        )
        assert refused.status_code == 422
        assert (await refused.get_json())["error"]["code"] == "access_grant_ack_required"
    ok = await client.post(
        url, headers=_admin(), json={"appId": _APP, "acknowledgedAccessGrants": _IDS}
    )
    assert ok.status_code == 201
