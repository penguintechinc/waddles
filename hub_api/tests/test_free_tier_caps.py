"""Free-tier entity caps (1 user-created community / tenant / admin) -- real paths.

Drives the real superadmin + user-management blueprints over a file-backed
sqlite DAL and a REAL `EntitlementClient` (fake PostHog/license gates only), so
tier resolution, the kill-switch flag, and outage degradation all run the
production code path. See `services/free_tier_caps.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import flask_core.feature_flags as feature_flags_module
import pytest
from flask_core.entitlement import EntitlementClient
from flask_core.feature_registry import FeatureRegistry
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.superadmin import superadmin_bp
from blueprints.v1.user_management import user_management_bp
from services import free_tier_caps, tenant_service
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
    async def test_catch_all_counts_so_free_cannot_create_a_second_tenant(
        self, client: Any, admin_db: Any, gates: Any
    ) -> None:
        # Free deployment whose only tenant is the catch-all: already at the cap.
        admin_db.dal(admin_db.dal.tenants.slug == TENANT_SLUG).update(slug="global", is_global=True)
        admin_db.dal.commit()
        headers = {
            "Authorization": "Bearer "
            + make_user_token(user_id=1, scope="*:admin", tenant="global")
        }
        blocked = await client.post(
            "/api/v1/superadmin/tenants", headers=headers, json={"slug": "t1", "displayName": "t1"}
        )
        assert blocked.status_code == 402
        _assert_upgrade_block(await blocked.get_json(), "tenant")
        slugs = {r.slug for r in admin_db.dal(admin_db.dal.tenants.id > 0).select()}
        assert slugs == {"global"}  # nothing created, nothing mutated

    async def test_only_system_inactive_excluded_from_count(
        self, admin_db: Any, gates: Any
    ) -> None:
        dal = admin_db.dal
        dal.tenants.insert(slug="system", display_name="System", is_active=True)
        dal.tenants.insert(slug="gone", display_name="X", is_active=False)
        dal.commit()
        assert free_tier_caps.count_user_created_tenants(dal) == 1  # acme only
        dal.tenants.insert(slug="global", display_name="G", is_global=True, is_active=True)
        dal.tenants.insert(slug="default", display_name="D", is_active=True)
        dal.commit()
        assert free_tier_caps.count_user_created_tenants(dal) == 3

    async def test_system_tenant_alone_leaves_first_tenant_creatable(
        self, client: Any, admin_db: Any, gates: Any
    ) -> None:
        dal = admin_db.dal
        dal.tenants.update_or_insert(dal.tenants.slug == TENANT_SLUG, slug="system")
        dal.commit()
        headers = {
            "Authorization": "Bearer "
            + make_user_token(user_id=1, scope="*:admin", tenant="system")
        }
        ok = await client.post(
            "/api/v1/superadmin/tenants", headers=headers, json={"slug": "t1", "displayName": "t1"}
        )
        assert ok.status_code == 201
        blocked = await client.post(
            "/api/v1/superadmin/tenants", headers=headers, json={"slug": "t2", "displayName": "t2"}
        )
        assert blocked.status_code == 402

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

    async def test_super_and_tenant_admins_count_platform_wide_for_grant(
        self, admin_db: Any, gates: Any
    ) -> None:
        seed_super_admin(admin_db, user_id=1)
        u2, u3 = _seed_user(admin_db, "u2@x.io"), _seed_user(admin_db, "u3@x.io")
        admin_db.dal.tenant_admins.insert(tenant_id=1, user_id=u2, role="tenant-admin")
        admin_db.dal.commit()
        assert free_tier_caps.admin_user_ids(admin_db.dal) == {u2}
        with pytest.raises(ApiError) as exc:
            await free_tier_caps.grant_super_admin_capped(
                admin_db,
                admin_db.dal,
                caller_tenant_slug=TENANT_SLUG,
                user_id=u3,
                update_fields={"is_super_admin": True},
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


def _seed_tenant(admin_db: Any, slug: str) -> int:
    tid: int = admin_db.dal.tenants.insert(slug=slug, display_name=slug, is_active=True)
    admin_db.dal.commit()
    return tid


class TestTargetTenantScope:
    """regression: gate/action mismatch -- tier resolves against the TARGET, not the caller."""

    @pytest.fixture
    def tiers(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        """Per-tenant tier map (``by_slug``) + every slug the cap gate resolved (``seen``)."""
        state = SimpleNamespace(by_slug={}, seen=[])

        async def fake_get_tier(*, tenant: str, community: int | None = None) -> str:
            state.seen.append(tenant)
            return str(state.by_slug.get(tenant, "free"))

        async def no_kill_switch(flag: str, *, tenant: str, **_: Any) -> bool:
            return False

        monkeypatch.setattr(free_tier_caps, "get_tier", fake_get_tier)
        monkeypatch.setattr(free_tier_caps, "feature_enabled", no_kill_switch)
        return state

    async def _add(self, admin_db: Any, target: str, target_id: int, uid: int) -> None:
        await tenant_service.add_tenant_admin(
            admin_db,
            admin_db.dal,
            tenant_id=target_id,
            user_id=uid,
            role="tenant-admin",
            tenant_slug=target,
        )

    async def test_paid_caller_adding_to_free_target_is_still_capped(
        self, admin_db: Any, tiers: SimpleNamespace
    ) -> None:
        free_id = _seed_tenant(admin_db, "freeco")
        tiers.by_slug[TENANT_SLUG] = "enterprise"  # the CALLER's tenant is paid
        u1, u2 = _seed_user(admin_db, "a@x.io"), _seed_user(admin_db, "b@x.io")
        await self._add(admin_db, "freeco", free_id, u1)
        with pytest.raises(ApiError) as exc:
            await self._add(admin_db, "freeco", free_id, u2)
        assert exc.value.status_code == 402
        assert "freeco" in tiers.seen
        assert TENANT_SLUG not in tiers.seen

    async def test_free_caller_adding_to_paid_target_is_not_capped(
        self, admin_db: Any, tiers: SimpleNamespace
    ) -> None:
        paid_id = _seed_tenant(admin_db, "paidco")
        tiers.by_slug["paidco"] = "professional"  # the TARGET is paid; caller (acme) is free
        for i in range(3):
            await self._add(admin_db, "paidco", paid_id, _seed_user(admin_db, f"p{i}@x.io"))
        assert free_tier_caps.count_tenant_admins(admin_db.dal, tenant_id=paid_id) == 3

    async def test_count_is_per_target_tenant(self, admin_db: Any, tiers: SimpleNamespace) -> None:
        a, b = _seed_tenant(admin_db, "ta"), _seed_tenant(admin_db, "tb")
        await self._add(admin_db, "ta", a, _seed_user(admin_db, "a@x.io"))
        await self._add(admin_db, "tb", b, _seed_user(admin_db, "b@x.io"))  # own slot, not blocked


class TestConcurrentCreatesAreAtomic:
    """regression: TOCTOU -- racing creates at the cap yield exactly one success + one 402."""

    async def test_racing_community_creates(self, client: Any, gates: Any) -> None:
        r = await asyncio.gather(_post_community(client, "a"), _post_community(client, "b"))
        assert sorted(x.status_code for x in r) == [201, 402]

    async def test_racing_tenant_creates(
        self, client: Any, gates: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `acme` (the caller's tenant) already counts, so raise the cap to 2: exactly one slot left.
        monkeypatch.setattr(free_tier_caps, "FREE_TIER_CAP", 2)
        r = await asyncio.gather(_post_tenant(client, "t1"), _post_tenant(client, "t2"))
        assert sorted(x.status_code for x in r) == [201, 402]

    async def test_racing_super_admin_grants(self, client: Any, admin_db: Any, gates: Any) -> None:
        seed_super_admin(admin_db, user_id=1)
        u2, u3 = _seed_user(admin_db, "u2@x.io"), _seed_user(admin_db, "u3@x.io")
        r = await asyncio.gather(_grant(client, u2), _grant(client, u3))
        assert sorted(x.status_code for x in r) == [200, 402]

    async def test_racing_tenant_admin_adds(self, admin_db: Any, gates: Any) -> None:
        tid = _tenant_id(admin_db)
        u1, u2 = _seed_user(admin_db, "a@x.io"), _seed_user(admin_db, "b@x.io")

        async def add(uid: int) -> str:
            try:
                await tenant_service.add_tenant_admin(
                    admin_db,
                    admin_db.dal,
                    tenant_id=tid,
                    user_id=uid,
                    role="tenant-admin",
                    tenant_slug=TENANT_SLUG,
                )
            except ApiError as exc:
                return str(exc.status_code)
            return "ok"

        assert sorted(await asyncio.gather(add(u1), add(u2))) == ["402", "ok"]
        assert free_tier_caps.count_tenant_admins(admin_db.dal, tenant_id=tid) == 1

    async def test_lock_then_count_then_write_in_one_job_on_postgres(self, gates: Any) -> None:
        events: list[str] = []

        class FakeDal:
            _dbname = "postgres"

            def executesql(self, sql: str, placeholders: list[Any] | None = None) -> None:
                events.append(f"lock:{placeholders}:{'pg_advisory_xact_lock' in sql}")

            def commit(self) -> None:
                events.append("commit")

            def rollback(self) -> None:
                events.append("rollback")

        fake_async = SimpleNamespace(executor=ThreadPoolExecutor(max_workers=1))

        def is_full(_: Any) -> bool:
            events.append("count")
            return False

        def write(_: Any) -> int:
            events.append("write")
            return 7

        out = await free_tier_caps.capped_write(
            fake_async,
            FakeDal(),
            kind="community",
            noun="community",
            tier_slug=TENANT_SLUG,
            lock_key="community:1",
            is_full=is_full,
            write=write,
        )
        assert out == 7
        assert events == ["lock:['free_cap:community:1']:True", "count", "write", "commit"]
