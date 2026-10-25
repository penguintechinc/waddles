"""Free-tier entity caps (1 user-created community / tenant / admin) -- real paths.

Drives the real superadmin + user-management blueprints over a file-backed
sqlite DAL and a REAL `EntitlementClient` (fake PostHog/license gates only), so
tier resolution, the kill-switch flag, and outage degradation all run the
production code path. See `services/free_tier_caps.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import flask_core.feature_flags as feature_flags_module
import pytest
from flask_core.entitlement import EntitlementClient
from flask_core.feature_registry import FeatureRegistry
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.superadmin import superadmin_bp
from blueprints.v1.user_management import user_management_bp
from services import free_tier_caps
from services.errors import ApiError
from tests.conftest import TENANT_SLUG, make_user_token, seed_super_admin

KILL_SWITCH = free_tier_caps.FLAG_DISABLE_FREE_TIER_CAPS


class FakeFlagGate:
    """PostHog stand-in: per-flag results; ``down`` simulates an outage (None)."""

    def __init__(self) -> None:
        """Start with no flags on and the server up."""
        self.on: set[str] = set()
        self.down = False

    def is_enabled(
        self, flag_key: str, distinct_id: str, *, groups: Mapping[str, str] | None = None
    ) -> bool | None:
        if self.down:
            return None
        return flag_key in self.on


class FakeLicenseGate:
    """License-server stand-in with a mutable tier and an outage switch."""

    def __init__(self, tier: str = "free") -> None:
        """Start up, on ``tier``."""
        self.tier = tier
        self.down = False

    def resolve_tier(self) -> str:
        if self.down:
            raise ConnectionError("license server unreachable")
        return self.tier


@pytest.fixture
def gates(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeFlagGate, FakeLicenseGate]:
    flag, lic = FakeFlagGate(), FakeLicenseGate()
    client = EntitlementClient(
        flag_gate=flag,
        license_gate=lic,
        tier_requirements={},
        community_tier_source=None,
        feature_registry=FeatureRegistry(),
    )
    monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: client)
    return flag, lic


@pytest.fixture
def app(admin_db: Any) -> Quart:
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(superadmin_bp)
    quart_app.register_blueprint(user_management_bp)
    quart_app.config["dal"] = admin_db.dal
    quart_app.config["async_dal"] = admin_db
    return quart_app


@pytest.fixture
def client(app: Quart) -> Any:
    return app.test_client()


def _headers() -> dict[str, str]:
    token = make_user_token(
        user_id=1, scope="*:read *:write *:admin *:delete users:admin", tenant=TENANT_SLUG
    )
    return {"Authorization": f"Bearer {token}"}


async def _post_community(client: Any, name: str) -> Any:
    return await client.post(
        "/api/v1/superadmin/communities",
        headers=_headers(),
        json={"name": name, "platform": "discord"},
    )


async def _post_tenant(client: Any, slug: str) -> Any:
    return await client.post(
        "/api/v1/superadmin/tenants",
        headers=_headers(),
        json={"slug": slug, "displayName": slug},
    )


def _seed_user(admin_db: Any, email: str) -> int:
    uid: int = admin_db.dal.hub_users.insert(
        email=email,
        username=email,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    admin_db.dal.commit()
    return uid


async def _grant(client: Any, user_id: int) -> Any:
    return await client.post(
        f"/api/v1/superadmin/users/{user_id}/super-admin-role",
        headers=_headers(),
        json={"grant": True},
    )


def _tenant_id(admin_db: Any) -> int:
    return int(admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).select().first().id)


def _assert_upgrade_block(body: dict[str, Any], cap: str) -> None:
    err = body["error"]
    assert err["code"] == "FREE_TIER_CAP_REACHED"
    assert "Upgrade" in err["message"]
    assert err["details"] == {
        "cap": cap,
        "limit": 1,
        "currentTier": "free",
        "requiredTier": "professional",
        "upgrade": True,
    }


class TestCommunityCap:
    async def test_free_first_ok_second_blocked_402(self, client: Any, gates: Any) -> None:
        assert (await _post_community(client, "first")).status_code == 201
        second = await _post_community(client, "second")
        assert second.status_code == 402
        _assert_upgrade_block(await second.get_json(), "community")

    async def test_global_and_other_tenant_communities_do_not_count(
        self, client: Any, admin_db: Any, gates: Any
    ) -> None:
        dal = admin_db.dal
        dal.communities.insert(
            name="global",
            display_name="Global",
            is_global=True,
            tenant_id=_tenant_id(admin_db),
            is_active=True,
        )
        dal.communities.insert(name="elsewhere", display_name="E", tenant_id=9999, is_active=True)
        dal.commit()
        assert (await _post_community(client, "first")).status_code == 201

    async def test_existing_community_never_mutated_by_block(
        self, client: Any, admin_db: Any, gates: Any
    ) -> None:
        await _post_community(client, "first")
        await _post_community(client, "second")
        rows = admin_db.dal(admin_db.dal.communities.id > 0).select()
        assert [r.name for r in rows] == ["first"]
        assert rows.first().is_active is not False

    @pytest.mark.parametrize("tier", ["professional", "enterprise"])
    async def test_paid_tiers_not_capped(self, client: Any, gates: Any, tier: str) -> None:
        gates[1].tier = tier
        for n in ("a", "b", "c"):
            assert (await _post_community(client, n)).status_code == 201


class TestTenantCap:
    async def test_free_first_ok_second_blocked(
        self, client: Any, admin_db: Any, gates: Any
    ) -> None:
        # The seeded acme-corp tenant is itself non-reserved; swap it for `default`
        # (the free baseline tenant) so the 1st user-created tenant is the new one.
        admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).update(slug="default")
        admin_db.dal.commit()
        headers = {
            "Authorization": "Bearer "
            + make_user_token(user_id=1, scope="*:admin", tenant="default")
        }
        ok = await client.post(
            "/api/v1/superadmin/tenants", headers=headers, json={"slug": "t1", "displayName": "t1"}
        )
        assert ok.status_code == 201
        blocked = await client.post(
            "/api/v1/superadmin/tenants", headers=headers, json={"slug": "t2", "displayName": "t2"}
        )
        assert blocked.status_code == 402
        _assert_upgrade_block(await blocked.get_json(), "tenant")
        slugs = {r.slug for r in admin_db.dal(admin_db.dal.tenants.id > 0).select()}
        assert slugs == {"default", "t1"}

    async def test_reserved_and_global_tenants_excluded(self, admin_db: Any, gates: Any) -> None:
        dal = admin_db.dal
        dal.tenants.insert(slug="system", display_name="System", is_active=True)
        dal.tenants.insert(slug="default", display_name="D", is_active=True)
        dal.tenants.insert(slug="global", display_name="Gl", is_active=True)
        dal.tenants.insert(slug="g", display_name="G", is_global=True, is_active=True)
        dal.tenants.insert(slug="gone", display_name="X", is_active=False)
        dal.commit()
        n = await free_tier_caps.count_user_created_tenants(admin_db, dal)
        assert n == 1  # only the seeded acme-corp

    async def test_paid_tier_not_capped(self, client: Any, gates: Any) -> None:
        gates[1].tier = "professional"
        assert (await _post_tenant(client, "t1")).status_code == 201
        assert (await _post_tenant(client, "t2")).status_code == 201


class TestAdminCap:
    async def test_bootstrap_free_first_ok_second_blocked(
        self, client: Any, admin_db: Any, gates: Any
    ) -> None:
        seed_super_admin(admin_db, user_id=1)  # bootstrap admin: not counted
        u2, u3 = _seed_user(admin_db, "u2@x.io"), _seed_user(admin_db, "u3@x.io")
        assert (await _grant(client, u2)).status_code == 200
        blocked = await _grant(client, u3)
        assert blocked.status_code == 402
        _assert_upgrade_block(await blocked.get_json(), "admin")
        assert bool(admin_db.dal.hub_users[u3].is_super_admin) is False
        assert bool(admin_db.dal.hub_users[u2].is_super_admin) is True  # untouched

    async def test_regrant_of_existing_admin_not_blocked(
        self, client: Any, admin_db: Any, gates: Any
    ) -> None:
        seed_super_admin(admin_db, user_id=1)
        u2 = _seed_user(admin_db, "u2@x.io")
        assert (await _grant(client, u2)).status_code == 200
        assert (await _grant(client, u2)).status_code == 200

    async def test_tenant_admin_counts_toward_cap(self, admin_db: Any, gates: Any) -> None:
        seed_super_admin(admin_db, user_id=1)
        u2, u3 = _seed_user(admin_db, "u2@x.io"), _seed_user(admin_db, "u3@x.io")
        admin_db.dal.tenant_admins.insert(tenant_id=1, user_id=u2, role="tenant-admin")
        admin_db.dal.commit()
        assert await free_tier_caps.admin_user_ids(admin_db, admin_db.dal) == {u2}
        with pytest.raises(ApiError) as exc:
            await free_tier_caps.enforce_admin_cap(
                admin_db, admin_db.dal, tenant_slug=TENANT_SLUG, target_user_id=u3
            )
        assert exc.value.status_code == 402

    async def test_paid_tier_not_capped(self, client: Any, admin_db: Any, gates: Any) -> None:
        gates[1].tier = "enterprise"
        seed_super_admin(admin_db, user_id=1)
        for i in range(3):
            assert (await _grant(client, _seed_user(admin_db, f"p{i}@x.io"))).status_code == 200


class TestKillSwitchAndOutage:
    async def test_kill_switch_on_bypasses_every_cap(
        self, client: Any, admin_db: Any, gates: Any
    ) -> None:
        gates[0].on.add(KILL_SWITCH)
        for n in ("a", "b"):
            assert (await _post_community(client, n)).status_code == 201
        for s in ("t1", "t2"):
            assert (await _post_tenant(client, s)).status_code == 201
        seed_super_admin(admin_db, user_id=1)
        for i in range(2):
            assert (await _grant(client, _seed_user(admin_db, f"k{i}@x.io"))).status_code == 200

    async def test_license_and_flag_outage_fail_closed_to_free_never_crash(
        self, client: Any, gates: Any
    ) -> None:
        gates[0].down = True
        gates[1].down = True
        assert (await _post_community(client, "first")).status_code == 201
        assert (await _post_community(client, "second")).status_code == 402

    async def test_outage_after_paid_uses_cached_tier(self, client: Any, gates: Any) -> None:
        gates[1].tier = "professional"
        assert (await _post_community(client, "a")).status_code == 201
        gates[1].down = True  # license server drops; last-known `professional` is served
        assert (await _post_community(client, "b")).status_code == 201
