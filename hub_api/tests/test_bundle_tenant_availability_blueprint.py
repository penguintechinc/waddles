"""Blueprint tests for the TENANT tier -- `/api/v1/apps/tenant/<slug>/availability`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_tenant_availability import BLUEPRINTS
from services.bundle_approval_service import install_version_globally
from tests.conftest import TENANT_SLUG, make_token

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
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


async def test_list_availability_any_tenant_member(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.get(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["bundles"] == []


async def test_enable_requires_tenant_admin(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    assert response.status_code == 403


async def test_enable_requires_a_current_global_install(app: Quart) -> None:
    token = make_token(scope="tenant:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    assert response.status_code == 409


async def test_enable_happy_path(app: Quart, install_dal: Any) -> None:
    await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    token = make_token(scope="tenant:admin", user_id="1")
    client = app.test_client()
    response = await client.post(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    assert response.status_code == 201
    listing = await client.get(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = await listing.get_json()
    assert body["bundles"][0]["appId"] == "waddles.socials.music.default"
    assert body["bundles"][0]["available"] is True


async def test_disable_requires_tenant_admin(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.delete(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability/waddles.socials.music.default",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_disable_unknown_is_404(app: Quart) -> None:
    token = make_token(scope="tenant:admin", user_id="1")
    client = app.test_client()
    response = await client.delete(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability/waddles.socials.music.default",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


async def test_disable_happy_path(app: Quart, install_dal: Any) -> None:
    await install_version_globally(
        install_dal, app_id="waddles.socials.music.default", version="3.0.1", installed_by=1
    )
    token = make_token(scope="tenant:admin", user_id="1")
    client = app.test_client()
    await client.post(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    response = await client.delete(
        f"/api/v1/apps/tenant/{TENANT_SLUG}/availability/waddles.socials.music.default",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


async def test_cross_tenant_slug_is_403(app: Quart) -> None:
    token = make_token(scope="tenant:admin", user_id="1", tenant=TENANT_SLUG)
    client = app.test_client()
    response = await client.post(
        "/api/v1/apps/tenant/some-other-tenant/availability",
        headers={"Authorization": f"Bearer {token}"},
        json={"appId": "waddles.socials.music.default"},
    )
    assert response.status_code == 403
