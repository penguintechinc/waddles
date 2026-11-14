"""`blueprints/v1/overlay.py` -- the M7 overlay group (Streaming module).

Standalone Quart app registering only `overlay_bp` (mirrors
`test_v1_auth_blueprint.py`'s own pattern) against the `overlay_db`
fixture (`tests/conftest.py`) -- real JWTs via `flask_core.auth.
create_jwt_token`, real pydal queries, no mocking of the auth chain.

Fail-first proof (executed, not narrated): temporarily commented out the
`await community_access.require_community_admin(...)` call inside
`blueprints/v1/overlay.py::_require_admin` (leaving only the `require_scope`
check) -- `test_get_overlay_non_member_with_scope_is_403` AND
`test_get_overlay_cross_tenant_community_is_403` both went red (200
instead of 403, a real IDOR: a caller holding the global
`streaming.overlay:admin` scope could read/create ANY community's overlay
token, including one in a different tenant); reverted, both green again.
Separately, removed the `Authorization` header check path (called the
route with no headers at all) -- `test_get_overlay_without_token_is_401`
confirmed 401 from `tenant_middleware` before any handler code runs.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from flask_core.tenancy import DEFAULT_TENANT_SLUG
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.overlay as overlay_module
from blueprints.v1.overlay import SCOPE_ADMIN, overlay_bp
from config import HubAPIConfig
from tests.conftest import (
    OTHER_TENANT_SLUG,
    TENANT_SLUG,
    make_super_admin_token,
    make_user_token,
    seed_community,
    seed_membership,
    seed_super_admin,
)


def _test_config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug=DEFAULT_TENANT_SLUG,
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin="http://localhost:5173",
        log_level="INFO",
        overlay_base_url="https://overlay.example.test",
    )


@pytest.fixture
def app(overlay_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(overlay_bp)
    quart_app.config["dal"] = overlay_db.dal
    quart_app.config["async_dal"] = overlay_db
    quart_app.config["HUB_API_CONFIG"] = _test_config()
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


@pytest.fixture(autouse=True)
def _feature_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Default the `streaming.overlays` two-gate Feature flag ON for every test in this file."""
    stub = AsyncMock(return_value=True)
    monkeypatch.setattr(overlay_module, "feature_enabled", stub)
    return stub


def _owner_headers(
    overlay_db: Any, *, tenant: str = TENANT_SLUG
) -> tuple[dict[str, str], int, int]:
    """Seed a community + an owner membership; return (headers, community_id, user_id)."""
    community_id = seed_community(overlay_db, tenant_slug=tenant)
    user_id = 501
    seed_membership(overlay_db, community_id=community_id, user_id=user_id)
    token = make_user_token(user_id=user_id, scope=SCOPE_ADMIN, tenant=tenant)
    return {"Authorization": f"Bearer {token}"}, community_id, user_id


class TestGetOverlay:
    async def test_get_overlay_without_token_is_401(self, client: Any) -> None:
        response = await client.get("/api/v1/admin/1/overlay")
        assert response.status_code == 401

    async def test_get_overlay_missing_scope_is_403(self, client: Any, overlay_db: Any) -> None:
        community_id = seed_community(overlay_db)
        seed_membership(overlay_db, community_id=community_id, user_id=42)
        token = make_user_token(user_id=42, scope="")  # no streaming.overlay:admin
        response = await client.get(
            f"/api/v1/admin/{community_id}/overlay",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_get_overlay_non_member_with_scope_is_403(
        self, client: Any, overlay_db: Any
    ) -> None:
        """Right scope, but caller has NO community_members row at all -- the core IDOR fix."""
        community_id = seed_community(overlay_db)
        token = make_user_token(user_id=99, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/overlay",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_get_overlay_member_but_not_admin_role_is_403(
        self, client: Any, overlay_db: Any
    ) -> None:
        """An active member with role='community-member' (not owner/admin) is still refused."""
        community_id = seed_community(overlay_db)
        seed_membership(overlay_db, community_id=community_id, user_id=7, role="community-member")
        token = make_user_token(user_id=7, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/overlay",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_get_overlay_cross_tenant_community_is_403(
        self, client: Any, overlay_db: Any
    ) -> None:
        """A real owner membership, but the community belongs to a DIFFERENT tenant."""
        community_id = seed_community(overlay_db, tenant_slug=TENANT_SLUG)
        seed_membership(overlay_db, community_id=community_id, user_id=55)
        # Token carries a DIFFERENT tenant -- tenant_middleware resolves ctx
        # against OTHER_TENANT_SLUG, and community_access's tenant_scoped()
        # check must refuse this community_id even though the membership row
        # itself exists.
        token = make_user_token(user_id=55, scope=SCOPE_ADMIN, tenant=OTHER_TENANT_SLUG)
        response = await client.get(
            f"/api/v1/admin/{community_id}/overlay",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_get_overlay_super_admin_bypasses_membership_check(
        self, client: Any, overlay_db: Any
    ) -> None:
        """A super_admin JWT bypasses BOTH the tenant check and the membership check."""
        community_id = seed_community(overlay_db)  # no membership row for this user at all
        seed_super_admin(overlay_db, user_id=1)
        token = make_super_admin_token(user_id=1, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/overlay",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    async def test_get_overlay_creates_then_returns_same_key(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, community_id, _ = _owner_headers(overlay_db)
        overlay_db.dal.executesql("ALTER TABLE communities ADD COLUMN overlay_code VARCHAR(16)")
        overlay_db.dal.commit()
        first = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        assert first.status_code == 200
        first_body = await first.get_json()
        assert first_body["success"] is True
        overlay = first_body["overlay"]
        assert len(overlay["overlay_key"]) == 64
        code = overlay_db.dal.executesql(
            f"SELECT overlay_code FROM communities WHERE id = {community_id}"  # noqa: S608
        )[0][0]
        # The Rust router serves only `/{overlay_code}/{surface}?key=`; never the legacy `/{key}`.
        assert overlay["overlayUrl"] == (
            f"https://overlay.example.test/{code}/alert_box?key={overlay['overlay_key']}"
        )
        assert overlay["is_active"] is True
        assert overlay["enabled_sources"] == ["alerts", "chat", "goals", "ticker"]

        second = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        second_body = await second.get_json()
        assert second_body["overlay"]["overlay_key"] == overlay["overlay_key"]
        assert second_body["overlay"]["id"] == overlay["id"]


class TestUpdateOverlay:
    async def test_update_overlay_no_fields_is_400(self, client: Any, overlay_db: Any) -> None:
        headers, community_id, _ = _owner_headers(overlay_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/overlay", headers=headers, json={}
        )
        assert response.status_code == 400

    async def test_update_overlay_not_found_is_404(self, client: Any, overlay_db: Any) -> None:
        headers, community_id, _ = _owner_headers(overlay_db)
        response = await client.put(
            f"/api/v1/admin/{community_id}/overlay",
            headers=headers,
            json={"isActive": False},
        )
        assert response.status_code == 404

    async def test_update_overlay_active_flag(self, client: Any, overlay_db: Any) -> None:
        headers, community_id, _ = _owner_headers(overlay_db)
        await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)  # create
        response = await client.put(
            f"/api/v1/admin/{community_id}/overlay",
            headers=headers,
            json={
                "isActive": False,
                "enabledSources": ["chat"],
                "themeConfig": {"color": "gold"},
            },
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["overlay"]["is_active"] is False
        assert body["overlay"]["enabled_sources"] == ["chat"]
        assert body["overlay"]["theme_config"] == {"color": "gold"}


class TestRotateOverlayKey:
    async def test_rotate_overlay_key_not_found_is_404(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, community_id, _ = _owner_headers(overlay_db)
        response = await client.post(
            f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers
        )
        assert response.status_code == 404

    async def test_rotate_overlay_key_changes_key(self, client: Any, overlay_db: Any) -> None:
        headers, community_id, _ = _owner_headers(overlay_db)
        created = await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        old_key = (await created.get_json())["overlay"]["overlay_key"]

        response = await client.post(
            f"/api/v1/admin/{community_id}/overlay/rotate", headers=headers
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body["overlay"]["overlay_key"] != old_key
        assert body["overlay"]["previous_key"] == old_key
        assert "grace period" not in body["message"]  # sanity: message text is the Node string
        assert body["message"] == "Overlay key rotated. Previous key valid for 5 more minutes."


class TestGetOverlayStats:
    async def test_get_overlay_stats_non_member_is_403(
        self, client: Any, overlay_db: Any
    ) -> None:
        community_id = seed_community(overlay_db)
        token = make_user_token(user_id=123, scope=SCOPE_ADMIN)
        response = await client.get(
            f"/api/v1/admin/{community_id}/overlay/stats",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_get_overlay_stats_invalid_days_defaults_to_seven(
        self, client: Any, overlay_db: Any
    ) -> None:
        headers, community_id, _ = _owner_headers(overlay_db)
        await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)
        response = await client.get(
            f"/api/v1/admin/{community_id}/overlay/stats?days=not-a-number", headers=headers
        )
        assert response.status_code == 200

    async def test_get_overlay_stats_empty(self, client: Any, overlay_db: Any) -> None:
        headers, community_id, _ = _owner_headers(overlay_db)
        await client.get(f"/api/v1/admin/{community_id}/overlay", headers=headers)  # create
        response = await client.get(f"/api/v1/admin/{community_id}/overlay/stats", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert body["stats"]["total_access_count"] == 0
        assert body["stats"]["daily"] == []

    async def test_get_overlay_stats_aggregates_daily_log(
        self, client: Any, overlay_db: Any
    ) -> None:
        from datetime import UTC, datetime

        headers, community_id, _ = _owner_headers(overlay_db)
        dal = overlay_db.dal
        dal.overlay_access_log.insert(
            community_id=community_id,
            overlay_key="k",
            ip_address="1.2.3.4",
            accessed_at=datetime.now(UTC),
        )
        dal.overlay_access_log.insert(
            community_id=community_id,
            overlay_key="k",
            ip_address="5.6.7.8",
            accessed_at=datetime.now(UTC),
        )
        dal.commit()

        response = await client.get(f"/api/v1/admin/{community_id}/overlay/stats", headers=headers)
        assert response.status_code == 200
        body = await response.get_json()
        assert len(body["stats"]["daily"]) == 1
        assert body["stats"]["daily"][0]["access_count"] == 2
        assert body["stats"]["daily"][0]["unique_ips"] == 2
