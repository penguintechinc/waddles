"""Blueprint tests for GET /api/v1/admin/bundle-versions (global-admin approval queue)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_admin import BLUEPRINTS
from tests.conftest import make_token


async def _insert_upload(
    install_dal: Any,
    *,
    app_id: str,
    version: str,
    status: str,
    reject_reason: str | None = None,
) -> None:
    now = datetime.now(UTC)
    await install_dal.app_version_uploads.async_insert(
        app_id=app_id,
        version=version,
        tenant_id=1,
        requested_by=7,
        artifact_kind="prebuilt",
        language="python",
        status=status,
        reject_reason=reject_reason,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    app = Quart(__name__)
    QuartSchema(app)
    app.config["async_dal"] = bundle_install_db
    app.config["dal"] = bundle_install_db.dal
    app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


async def test_requires_platform_admin_scope(app: Quart) -> None:
    token = make_token(scope="")
    client = app.test_client()
    response = await client.get(
        "/api/v1/admin/bundle-versions", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


async def test_default_status_lists_only_published_as_pending(app: Quart, install_dal: Any) -> None:
    await _insert_upload(
        install_dal, app_id="waddles.integrations.vendor-1.a", version="1.0.0", status="PUBLISHED"
    )
    await _insert_upload(
        install_dal, app_id="waddles.integrations.vendor-2.b", version="1.0.0", status="UPLOADED"
    )
    await _insert_upload(
        install_dal,
        app_id="waddles.integrations.vendor-3.c",
        version="1.0.0",
        status="REJECTED",
        reject_reason="bad manifest",
    )
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.get(
        "/api/v1/admin/bundle-versions", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert body["success"] is True
    assert len(body["versions"]) == 1
    assert body["versions"][0]["appId"] == "waddles.integrations.vendor-1.a"
    assert body["versions"][0]["status"] == "PUBLISHED"
    assert body["pagination"]["total"] == 1


async def test_status_query_param_passthrough(app: Quart, install_dal: Any) -> None:
    await _insert_upload(
        install_dal,
        app_id="waddles.integrations.vendor-3.c",
        version="1.0.0",
        status="REJECTED",
        reject_reason="bad manifest",
    )
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.get(
        "/api/v1/admin/bundle-versions?status=rejected",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = await response.get_json()
    assert len(body["versions"]) == 1
    assert body["versions"][0]["rejectReason"] == "bad manifest"


async def test_pagination(app: Quart, install_dal: Any) -> None:
    for i in range(3):
        await _insert_upload(
            install_dal,
            app_id=f"waddles.integrations.vendor-{i}.x",
            version="1.0.0",
            status="PUBLISHED",
        )
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.get(
        "/api/v1/admin/bundle-versions?limit=2&page=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = await response.get_json()
    assert len(body["versions"]) == 2
    assert body["pagination"] == {"page": 1, "limit": 2, "total": 3, "totalPages": 2}


async def test_response_matches_dto_shape(app: Quart, install_dal: Any) -> None:
    """`@validate_response(ListBundleVersionsResponse)` -- exact field set, no drift."""
    await _insert_upload(
        install_dal, app_id="waddles.integrations.vendor-1.a", version="1.0.0", status="PUBLISHED"
    )
    token = make_token(scope="platform:admin")
    client = app.test_client()
    response = await client.get(
        "/api/v1/admin/bundle-versions", headers={"Authorization": f"Bearer {token}"}
    )
    body = await response.get_json()
    assert set(body.keys()) == {"success", "versions", "pagination"}
    assert set(body["versions"][0].keys()) == {
        "versionId",
        "appId",
        "version",
        "status",
        "requestedBy",
        "createdAt",
        "rejectReason",
    }
