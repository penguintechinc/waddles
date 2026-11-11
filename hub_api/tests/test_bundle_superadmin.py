"""Blueprint tests for /api/v1/superadmin/bundles (list, disable, enable)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.bundle_superadmin import BLUEPRINTS
from services.bundle_approval_service import install_version_globally
from tests.conftest import make_token

_SVC = "waddles.integrations.tools.svc"
_APP = "waddles.socials.music.default"
BASE = "/api/v1/superadmin/bundles"


def _manifest(app_id: str, version: str, **extra: Any) -> dict[str, Any]:
    """Minimal v2 manifest."""
    return {
        "schema_version": 2,
        "app_id": app_id,
        "name": f"Name of {app_id}",
        "version": version,
        "feature": "waddles.socials.music",
        "module": "socials",
        "provider": "builtin",
        "language": "python",
        "artifact": "source",
        "stages": {"process": {"entry": "x:y", "consumes": []}},
        **extra,
    }


async def _seed(
    dal: Any, install_dal: Any, app_id: str, version: str, manifest: dict[str, Any], kind: str
) -> None:
    """Insert the catalog row, stage + globally install one version."""
    dal.app_catalog.insert(
        app_id=app_id,
        name=app_id,
        manifest_version=version,
        module="socials",
        feature=".".join(app_id.split(".")[:3]),
        provider="builtin",
        execution_model="native",
        is_default=False,
        platform_compatibility={"tested_with": "3.0.0", "min_version": None, "max_version": None},
        status="active",
        stages={},
        kind=kind,
    )
    dal.commit()
    now = datetime.now(UTC)
    version_id = await install_dal.app_versions.async_insert(
        app_id=app_id,
        version=version,
        artifact_digest="sha256:" + "ab" * 32,
        language="python",
        artifact_kind="source",
        scan_status="scanned",
    )
    await install_dal.app_version_uploads.async_insert(
        app_id=app_id,
        version=version,
        tenant_id=1,
        artifact_kind="source",
        language="python",
        status="PUBLISHED",
        manifest_json=manifest,
        app_version_id=version_id,
        created_at=now,
        updated_at=now,
    )
    await install_version_globally(install_dal, app_id=app_id, version=version, installed_by=1)


@pytest.fixture
async def app(bundle_install_db: Any, install_dal: Any) -> Quart:
    """App with one service bundle and one app bundle requesting the service's tool."""
    dal = bundle_install_db.dal
    await _seed(
        dal, install_dal, _SVC, "1.0.0", _manifest(_SVC, "1.0.0", kind="service"), "service"
    )
    await _seed(
        dal,
        install_dal,
        _APP,
        "2.0.0",
        _manifest(
            _APP,
            "2.0.0",
            requested_tools=[{"target_app_id": _SVC, "tool": "do.it", "access": "read"}],
        ),
        "app",
    )
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.config["async_dal"] = bundle_install_db
    quart_app.config["dal"] = bundle_install_db.dal
    quart_app.config["install_dal"] = install_dal
    for bp in BLUEPRINTS:
        quart_app.register_blueprint(bp)
    return quart_app


def _hdr(scope: str = "platform:admin") -> dict[str, str]:
    """Bearer header for numeric user 1."""
    return {"Authorization": f"Bearer {make_token(scope=scope, user_id='1')}"}


async def test_list_filters_by_kind_with_exact_fields(app: Quart) -> None:
    """kind=service returns only the service, with the exact DTO field set."""
    r = await app.test_client().get(f"{BASE}?kind=service", headers=_hdr())
    assert r.status_code == 200
    body = await r.get_json()
    assert [b["appId"] for b in body["bundles"]] == [_SVC]
    assert set(body["bundles"][0]) == {
        "appId",
        "name",
        "kind",
        "version",
        "digestShort",
        "installedAt",
        "installSource",
        "status",
    }
    assert body["bundles"][0]["version"] == "1.0.0"
    assert body["bundles"][0]["digestShort"] == "sha256:" + "ab" * 6
    assert body["bundles"][0]["status"] == "active"


async def test_list_apps_and_invalid_kind(app: Quart) -> None:
    """kind=app excludes services; a bogus kind is a 400."""
    client = app.test_client()
    body = await (await client.get(f"{BASE}?kind=app", headers=_hdr())).get_json()
    assert [b["appId"] for b in body["bundles"]] == [_APP]
    assert (await client.get(f"{BASE}?kind=bogus", headers=_hdr())).status_code == 400


async def test_unauthorized_403(app: Quart) -> None:
    """Non platform:admin tokens are refused on every route."""
    client = app.test_client()
    assert (await client.get(BASE, headers=_hdr(""))).status_code == 403
    assert (await client.post(f"{BASE}/{_SVC}/disable", headers=_hdr(""))).status_code == 403
    assert (await client.post(f"{BASE}/{_SVC}/enable", headers=_hdr(""))).status_code == 403


async def test_disable_with_dependents_409_then_force(app: Quart) -> None:
    """Dependents block disable with a typed 409; force=true succeeds; list shows disabled."""
    client = app.test_client()
    r = await client.post(f"{BASE}/{_SVC}/disable", headers=_hdr())
    assert r.status_code == 409
    err = (await r.get_json())["error"]
    assert err["code"] == "has_dependents"
    assert err["details"]["dependents"] == [{"appId": _APP, "kind": "app", "version": "2.0.0"}]

    r = await client.post(f"{BASE}/{_SVC}/disable?force=true", headers=_hdr())
    assert r.status_code == 200
    body = await r.get_json()
    assert set(body) == {"success", "appId", "status", "changed", "tenantsHidden"}
    assert body["status"] == "disabled" and body["changed"] is True
    listing = await (await client.get(f"{BASE}?kind=service", headers=_hdr())).get_json()
    assert listing["bundles"][0]["status"] == "disabled"


async def test_disable_idempotent_and_enable(app: Quart) -> None:
    """Re-disable is a no-op 200; enable restores active; re-enable is a no-op."""
    client = app.test_client()
    await client.post(f"{BASE}/{_SVC}/disable?force=true", headers=_hdr())
    again = await (await client.post(f"{BASE}/{_SVC}/disable", headers=_hdr())).get_json()
    assert again["changed"] is False
    en = await (await client.post(f"{BASE}/{_SVC}/enable", headers=_hdr())).get_json()
    assert en["status"] == "active" and en["changed"] is True
    en2 = await (await client.post(f"{BASE}/{_SVC}/enable", headers=_hdr())).get_json()
    assert en2["changed"] is False


async def test_disable_app_bundle_422_and_unknown_404(app: Quart) -> None:
    """Only service bundles toggle; unknown ids 404."""
    client = app.test_client()
    assert (await client.post(f"{BASE}/{_APP}/disable", headers=_hdr())).status_code == 422
    assert (await client.post(f"{BASE}/nope.x.y.z/disable", headers=_hdr())).status_code == 404


async def test_disable_writes_audit_row(app: Quart, install_dal: Any) -> None:
    """A disable leaves a service_bundle_disabled audit row."""
    await app.test_client().post(f"{BASE}/{_SVC}/disable?force=true", headers=_hdr())
    rows = await install_dal(install_dal.audit_log.action == "service_bundle_disabled").select()
    assert rows.first() is not None


async def test_disabled_bundle_cannot_reenter_tenant_or_community(
    app: Quart, install_dal: Any
) -> None:
    """Security: set_available and activation both 409 `bundle_disabled`; re-enable restores."""
    from services import bundle_approval_service as approvals
    from services import tenant_app_availability_service as availability
    from services.errors import ApiError

    client = app.test_client()
    await availability.set_available(install_dal, tenant_id=1, app_id=_SVC, updated_by=1)
    community_id = int(await install_dal.communities.async_insert(tenant_id=1, name="acme"))

    # Simulate the cascade race: status flips but availability/activation rows linger.
    await install_dal(install_dal.app_catalog.app_id == _SVC).update(status="disabled")
    with pytest.raises(ApiError) as tenant_exc:
        await availability.set_available(install_dal, tenant_id=1, app_id=_SVC, updated_by=1)
    assert (tenant_exc.value.status_code, tenant_exc.value.code) == (409, "bundle_disabled")
    with pytest.raises(ApiError) as community_exc:
        await approvals.activate_for_community(
            install_dal,
            tenant_id=1,
            community_id=community_id,
            app_id=_SVC,
            activated_by=1,
        )
    assert (community_exc.value.status_code, community_exc.value.code) == (409, "bundle_disabled")

    enabled = await client.post(f"{BASE}/{_SVC}/enable", headers=_hdr())
    assert (await enabled.get_json())["status"] == "active"
    row = await availability.set_available(install_dal, tenant_id=1, app_id=_SVC, updated_by=1)
    assert row.available
