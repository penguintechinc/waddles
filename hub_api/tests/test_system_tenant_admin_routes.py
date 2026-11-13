"""Security review #1/#3: the `system` tenant's admins are managed ONLY via the superadmin route.

A `system` tenant admin IS a global admin, so the tenant-scoped add/remove routes would bypass
the superadmin path's DB re-check, last-admin guard, session revocation and audit. Both routes
refuse with a typed 403 pointing at `POST /api/v1/superadmin/users/<id>/super-admin-role`.
"""

from __future__ import annotations

from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.tenant import tenant_bp
from services.global_identity import grant_global_admin_sync, is_global_admin_sync
from tests.conftest import TENANT_SLUG
from tests.test_v1_tenant_blueprint import _admin_headers, _seed_user

CODE = "SYSTEM_TENANT_ADMIN_VIA_SUPERADMIN_ONLY"


@pytest.fixture
def app(tenant_admin_db: Any) -> Quart:
    """Standalone app mounting only `tenant_bp`."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(tenant_bp)
    quart_app.config["dal"] = tenant_admin_db.dal
    quart_app.config["async_dal"] = tenant_admin_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    """Test client for `app`."""
    return app.test_client()


def _seed_system(tenant_admin_db: Any) -> tuple[int, int]:
    """Seed `system` with one global admin; return (admin_id, plain_user_id)."""
    admin_id = _seed_user(tenant_admin_db, email="root@example.com")
    grant_global_admin_sync(tenant_admin_db.dal, admin_id)
    tenant_admin_db.dal.commit()
    return admin_id, _seed_user(tenant_admin_db, email="other@example.com")


async def test_add_system_admin_via_tenant_route_is_403_typed(
    client: Any, tenant_admin_db: Any
) -> None:
    """Adding a `system` admin through the tenant API is refused and creates no admin."""
    admin_id, other = _seed_system(tenant_admin_db)
    response = await client.post(
        "/api/v1/tenant/system/admins",
        headers=_admin_headers(user_id=admin_id, tenant="system"),
        json={"userId": other, "role": "tenant-owner"},
    )
    assert response.status_code == 403
    body = await response.get_json()
    assert body["error"]["code"] == CODE, body
    assert "super-admin-role" in str(body)
    assert is_global_admin_sync(tenant_admin_db.dal, other) is False


async def test_remove_system_admin_via_tenant_route_is_403_typed(
    client: Any, tenant_admin_db: Any
) -> None:
    """Removing the (last) `system` admin through the tenant API is refused; they survive."""
    admin_id, _ = _seed_system(tenant_admin_db)
    response = await client.delete(
        f"/api/v1/tenant/system/admins/{admin_id}",
        headers=_admin_headers(user_id=admin_id, tenant="system"),
    )
    assert response.status_code == 403
    body = await response.get_json()
    assert body["error"]["code"] == CODE, body
    assert is_global_admin_sync(tenant_admin_db.dal, admin_id) is True


async def test_ordinary_tenant_admin_add_and_remove_still_work(
    client: Any, tenant_admin_db: Any
) -> None:
    """The guard is scoped to `system`: a normal tenant still adds and removes admins."""
    user_id = _seed_user(tenant_admin_db)
    added = await client.post(
        f"/api/v1/tenant/{TENANT_SLUG}/admins",
        headers=_admin_headers(),
        json={"userId": user_id, "role": "tenant-admin"},
    )
    assert added.status_code == 200
    removed = await client.delete(
        f"/api/v1/tenant/{TENANT_SLUG}/admins/{user_id}", headers=_admin_headers()
    )
    assert removed.status_code == 200
