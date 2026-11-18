"""Operator license-pool routes: a tenant owner can never reach them (adversarial review C).

`tenants:admin` is only in the global bundle's `*:admin`; the tenant-owner bundle never holds it.
Defence in depth: even a token holding it must belong to the reserved `system` tenant.
"""

from __future__ import annotations

from typing import Any

import pytest
from flask_core.auth import SCOPE_BUNDLES
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.superadmin_license_pools import superadmin_license_pools_bp
from services.schema import bind_community_tier_tables
from tests.conftest import TENANT_SLUG, make_user_token

BYPASS_HOST = "alpha.penguintech.cloud"


@pytest.fixture
def env(tenant_admin_db: Any) -> Any:
    dal = tenant_admin_db.dal
    bind_community_tier_tables(dal, migrate=True)
    dal.tenants.insert(slug="system", display_name="System", is_active=True)
    dal.commit()
    dal(dal.tenant_license_pools).count()
    tenant_id = int(dal(dal.tenants.slug == TENANT_SLUG).select().first().id)
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(superadmin_license_pools_bp)
    app.config["dal"] = dal
    app.config["async_dal"] = tenant_admin_db
    return type("Env", (), {"client": app.test_client(), "tenant": tenant_id})


def _hdr(scope: str, tenant: str) -> dict[str, str]:
    token = make_user_token(user_id=1, scope=scope, tenant=tenant)
    return {"Authorization": f"Bearer {token}", "Host": BYPASS_HOST}


_OWNER = " ".join(sorted({*SCOPE_BUNDLES["global"]["viewer"], *SCOPE_BUNDLES["tenant"]["admin"]}))


@pytest.mark.parametrize("tenant", [TENANT_SLUG, "system"])
async def test_tenant_owner_bundle_is_403_on_get_and_put(env: Any, tenant: str) -> None:
    url = f"/api/v1/superadmin/tenants/{env.tenant}/license-pools"
    assert (await env.client.get(url, headers=_hdr(_OWNER, tenant))).status_code == 403
    put = await env.client.put(f"{url}/2", headers=_hdr(_OWNER, tenant), json={"limit": -1})
    assert put.status_code == 403


async def test_tenants_admin_in_a_non_system_tenant_is_403(env: Any) -> None:
    url = f"/api/v1/superadmin/tenants/{env.tenant}/license-pools"
    headers = _hdr("*:admin tenants:admin", TENANT_SLUG)
    assert (await env.client.get(url, headers=headers)).status_code == 403
    assert (await env.client.put(f"{url}/2", headers=headers, json={"limit": 5})).status_code == 403


async def test_system_tenant_global_admin_still_allowed(env: Any) -> None:
    url = f"/api/v1/superadmin/tenants/{env.tenant}/license-pools"
    headers = _hdr("*:admin", "system")
    put = await env.client.put(f"{url}/2", headers=headers, json={"limit": 5})
    assert put.status_code == 200
    assert (await env.client.get(url, headers=headers)).status_code == 200
