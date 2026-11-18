"""`blueprints/v1/tenant_community_tiers.py` -- tenant-admin community tier allocation API.

Real pieces everywhere except the license server: a file-backed DAL, the real
`community_tier_service` + `StorePoolProvider` over the real `tenant_license_pools` table (injected
through the `POOL_PROVIDER` app-config seam), real scope checks on real JWTs.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.tenant_community_tiers import tenant_community_tiers_bp
from services import community_tier_service as svc
from services.schema import bind_community_tier_tables
from tests.conftest import TENANT_SLUG, make_user_token

PRO, ENT, FREE = svc.TIER_CODE_PROFESSIONAL, svc.TIER_CODE_ENTERPRISE, svc.TIER_CODE_FREE
OTHER_SLUG = "other-corp"
BASE = f"/api/v1/tenant/{TENANT_SLUG}"
FULL_SCOPE = "tenant:admin tenant.community_tier:read tenant.community_tier:allocate"


class RecordingAudit:
    """In-memory audit sink with the `AuditService.record` surface."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def flag(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Controllable rollout flag (default ON for these tests; flipped per test)."""
    state = {"on": True}

    async def _enabled(key: str, **_: Any) -> bool:
        assert key == "waddles.community-tier-allocation"
        return state["on"]

    monkeypatch.setattr("blueprints.v1.tenant_community_tiers.feature_enabled", _enabled)
    return state


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> RecordingAudit:
    sink = RecordingAudit()
    monkeypatch.setattr("blueprints.v1.tenant_community_tiers.get_audit_service", lambda: sink)
    return sink


@pytest.fixture
def env(admin_db: Any, flag: dict[str, bool], audit: RecordingAudit) -> Any:
    dal = admin_db.dal
    bind_community_tier_tables(dal, migrate=True)
    for name in ("community_tier_allocations", "tenant_license_pools"):
        dal(dal[name]).count()
    tenant_id = int(dal(dal.tenants.slug == TENANT_SLUG).select().first().id)
    other_id = int(dal.tenants.insert(slug=OTHER_SLUG, display_name="Other", is_active=True))
    cs = [
        (
            int(
                dal.communities.insert(
                    name=f"c{i}", tenant_id=tenant_id, public_id=str(uuid.uuid4())
                )
            ),
            None,
        )
        for i in range(4)
    ]
    ids = [c for c, _ in cs]
    pids = [str(dal.communities[c].public_id) for c in ids]
    foreign = int(
        dal.communities.insert(name="foreign", tenant_id=other_id, public_id=str(uuid.uuid4()))
    )
    dal.commit()
    store = svc.PoolStore(admin_db, dal)
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(tenant_community_tiers_bp)
    app.config["dal"] = dal
    app.config["async_dal"] = admin_db
    app.config["POOL_PROVIDER"] = svc.StorePoolProvider(store)
    yield type(
        "Env",
        (),
        {
            "app": app,
            "client": app.test_client(),
            "adal": admin_db,
            "dal": dal,
            "tenant": tenant_id,
            "other": other_id,
            "ids": ids,
            "pids": pids,
            "foreign_pid": str(dal.communities[foreign].public_id),
            "store": store,
            "audit": audit,
        },
    )
    svc._touch_seen.clear()


def _h(scope: str = FULL_SCOPE, tenant: str = TENANT_SLUG) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_user_token(user_id=1, scope=scope, tenant=tenant)}"}


async def _pool(env: Any, tier: int, limit: int | None) -> None:
    await env.store.put(env.tenant, tier, limit, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc())


async def _put_tier(env: Any, pid: str, tier: Any, **kw: Any) -> tuple[int, Any]:
    response = await env.client.put(
        f"{BASE}/communities/{pid}/tier", headers=_h(**kw), json={"tier": tier}
    )
    return response.status_code, await response.get_json()


def _active(env: Any, community_id: int) -> Any:
    t = env.dal.community_tier_allocations
    return env.dal((t.community_id == community_id) & (t.released_at == None)).select().first()  # noqa: E711


class TestPool:
    async def test_pool_read_exact_field_set(self, env: Any) -> None:
        await _pool(env, PRO, 2)
        await _pool(env, ENT, -1)
        r = await _put_tier(env, env.pids[0], PRO)
        assert r[0] == 200, r
        response = await env.client.get(f"{BASE}/license/pool", headers=_h())
        assert response.status_code == 200
        body = await response.get_json()
        assert set(body) == {"success", "tiers", "observedAt", "stale", "source"}
        assert body["stale"] is False and body["source"] == "operator_set"
        pro, ent = body["tiers"]
        assert set(pro) == {"tier", "limit", "used", "available", "overage"}
        assert (pro["tier"], pro["limit"], pro["used"], pro["available"]) == (PRO, 2, 1, 1)
        assert (ent["tier"], ent["limit"], ent["used"]) == (ENT, -1, 0)

    async def test_pool_not_licensed_tier_has_null_limit(self, env: Any) -> None:
        await _pool(env, PRO, None)
        await _pool(env, ENT, 0)
        body = await (await env.client.get(f"{BASE}/license/pool", headers=_h())).get_json()
        assert body["tiers"][0]["limit"] is None

    async def test_pool_unavailable_is_503_fail_loud(self, env: Any) -> None:
        response = await env.client.get(f"{BASE}/license/pool", headers=_h())
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "TEAM_POOL_UNAVAILABLE"

    async def test_pool_stale_flag_is_reported(self, env: Any) -> None:
        old = svc.now_utc() - timedelta(days=30)
        for tier in (PRO, ENT):
            await env.store.put(env.tenant, tier, 1, svc.POOL_SOURCE_LICENSE_SERVER, old)
        body = await (await env.client.get(f"{BASE}/license/pool", headers=_h())).get_json()
        assert body["stale"] is True and body["source"] == "license_server"


class TestAllocate:
    async def test_allocate_within_pool(self, env: Any) -> None:
        await _pool(env, PRO, 2)
        status, body = await _put_tier(env, env.pids[0], PRO)
        assert status == 200
        assert set(body) == {
            "success", "communityId", "tier", "previousTier", "changed", "used", "limit", "overage",
        }  # fmt: skip
        assert body["communityId"] == env.pids[0] and body["tier"] == PRO
        assert body["previousTier"] == FREE and body["changed"] is True
        assert (body["used"], body["limit"], body["overage"]) == (1, 2, 0)
        row = env.dal.communities[env.ids[0]]
        assert row.license_tier == "professional" and row.is_premium
        assert _active(env, env.ids[0]).source_code == svc.SOURCE_TENANT_ADMIN
        assert env.audit.events[-1].action == "license.community_tier_allocated"

    async def test_exhausted_pool_is_402_and_does_not_allocate(self, env: Any) -> None:
        await _pool(env, PRO, 1)
        await _put_tier(env, env.pids[0], PRO)
        status, body = await _put_tier(env, env.pids[1], PRO)
        assert status == 402
        assert body["error"]["code"] == "TEAM_POOL_EXHAUSTED"
        assert body["error"]["details"] == {
            "requiredTier": PRO, "limit": 1, "used": 1, "upgrade": True,
        }  # fmt: skip
        assert _active(env, env.ids[1]) is None
        assert env.audit.events[-1].action == "license.community_tier_allocation_blocked"

    async def test_stale_allocations_still_fill_the_pool_402_then_release_frees_one(
        self, env: Any
    ) -> None:
        """Regression: 31 days of inactivity must NOT free pool slots (pool cap defeat)."""
        await _pool(env, PRO, 2)
        await _pool(env, ENT, 0)
        assert (await _put_tier(env, env.pids[0], PRO))[0] == 200
        assert (await _put_tier(env, env.pids[1], PRO))[0] == 200
        t = env.dal.community_tier_allocations
        env.dal(t.tier_code == PRO).update(last_active_at=svc.now_utc() - timedelta(days=31))
        env.dal.commit()
        status, body = await _put_tier(env, env.pids[2], PRO)
        assert status == 402 and body["error"]["code"] == "TEAM_POOL_EXHAUSTED"
        assert body["error"]["details"]["used"] == 2 and body["error"]["details"]["limit"] == 2
        assert _active(env, env.ids[2]) is None
        pool = await (await env.client.get(f"{BASE}/license/pool", headers=_h())).get_json()
        assert (pool["tiers"][0]["used"], pool["tiers"][0]["available"]) == (2, 0)
        assert (await _put_tier(env, env.pids[0], FREE))[0] == 200
        assert (await _put_tier(env, env.pids[2], PRO))[0] == 200
        assert (await _put_tier(env, env.pids[3], PRO))[0] == 402

    async def test_unlicensed_tier_is_402_with_zero_limit(self, env: Any) -> None:
        await _pool(env, ENT, None)
        status, body = await _put_tier(env, env.pids[0], ENT)
        assert status == 402 and body["error"]["details"]["limit"] == 0

    async def test_release_frees_a_slot(self, env: Any) -> None:
        await _pool(env, PRO, 1)
        await _put_tier(env, env.pids[0], PRO)
        assert (await _put_tier(env, env.pids[1], PRO))[0] == 402
        status, body = await _put_tier(env, env.pids[0], FREE)
        assert status == 200 and body["changed"] is True and body["tier"] == FREE
        assert body["limit"] is None and body["used"] == 0
        assert (await _put_tier(env, env.pids[1], PRO))[0] == 200
        assert env.dal.communities[env.ids[0]].license_tier == "free"

    async def test_free_release_works_even_when_pool_unavailable(self, env: Any) -> None:
        status, body = await _put_tier(env, env.pids[0], FREE)
        assert status == 200 and body["changed"] is False

    async def test_pool_unavailable_is_503_and_nothing_allocated(self, env: Any) -> None:
        status, body = await _put_tier(env, env.pids[0], PRO)
        assert status == 503 and body["error"]["code"] == "TEAM_POOL_UNAVAILABLE"
        assert _active(env, env.ids[0]) is None

    async def test_pro_to_ent_change_keeps_old_slot_when_ent_full(self, env: Any) -> None:
        await _pool(env, PRO, 1)
        await _pool(env, ENT, 0)
        await _put_tier(env, env.pids[0], PRO)
        status, _ = await _put_tier(env, env.pids[0], ENT)
        assert status == 402
        assert _active(env, env.ids[0]).tier_code == PRO

    @pytest.mark.parametrize("tier", [0, 4, -1, "2", 2.5, True, None])
    async def test_invalid_tier_is_400(self, env: Any, tier: Any) -> None:
        await _pool(env, PRO, 5)
        status, _ = await _put_tier(env, env.pids[0], tier)
        assert status == 400
        assert _active(env, env.ids[0]) is None

    async def test_extra_body_field_is_400(self, env: Any) -> None:
        response = await env.client.put(
            f"{BASE}/communities/{env.pids[0]}/tier",
            headers=_h(),
            json={"tier": PRO, "isPremium": True},
        )
        assert response.status_code == 400

    async def test_cross_tenant_and_unknown_and_malformed_uuid_are_uniform_404(
        self, env: Any
    ) -> None:
        await _pool(env, PRO, 5)
        results = [
            await _put_tier(env, env.foreign_pid, PRO),
            await _put_tier(env, str(uuid.uuid4()), PRO),
            await _put_tier(env, "not-a-uuid", PRO),
            await _put_tier(env, str(env.ids[0]), PRO),  # a numeric id is never accepted
        ]
        assert {s for s, _ in results} == {404}
        assert len({b["error"]["message"] for _, b in results}) == 1

    async def test_uppercase_uuid_resolves(self, env: Any) -> None:
        await _pool(env, PRO, 5)
        status, body = await _put_tier(env, env.pids[0].upper(), PRO)
        assert status == 200 and body["communityId"] == env.pids[0]


class TestList:
    async def _seed(self, env: Any) -> None:
        await _pool(env, PRO, 5)
        await _pool(env, ENT, 5)
        await _put_tier(env, env.pids[0], PRO)
        await _put_tier(env, env.pids[1], ENT)
        await _put_tier(env, env.pids[2], PRO)

    async def _get(self, env: Any, qs: str = "", **kw: Any) -> tuple[int, Any]:
        response = await env.client.get(
            f"{BASE}/communities/tier-allocations{qs}", headers=_h(**kw)
        )
        return response.status_code, await response.get_json()

    async def test_list_exact_field_set_and_only_active(self, env: Any) -> None:
        await self._seed(env)
        await _put_tier(env, env.pids[2], FREE)  # released rows never list
        status, body = await self._get(env)
        assert status == 200
        assert set(body) == {"success", "allocations", "meta"}
        assert body["meta"] == {"total": 2, "limit": 50, "offset": 0}
        item = body["allocations"][0]
        assert set(item) == {"communityId", "tier", "source", "allocatedAt", "stale"}
        assert {a["communityId"] for a in body["allocations"]} == {env.pids[0], env.pids[1]}
        assert all(
            a["source"] == svc.SOURCE_TENANT_ADMIN and a["stale"] is False
            for a in body["allocations"]
        )

    async def test_filters_and_pagination(self, env: Any) -> None:
        await self._seed(env)
        _, by_tier = await self._get(env, f"?tier={PRO}")
        assert by_tier["meta"]["total"] == 2
        assert {a["tier"] for a in by_tier["allocations"]} == {PRO}
        _, none = await self._get(env, f"?source={svc.SOURCE_OPERATOR_OVERRIDE}")
        assert none["allocations"] == [] and none["meta"]["total"] == 0
        _, empty_tier = await self._get(env, f"?tier={FREE}")
        assert empty_tier["allocations"] == []
        _, page = await self._get(env, "?limit=1&offset=1")
        assert len(page["allocations"]) == 1 and page["meta"] == {
            "total": 3,
            "limit": 1,
            "offset": 1,
        }
        _, combo = await self._get(env, f"?tier={ENT}&source={svc.SOURCE_TENANT_ADMIN}")
        assert [a["communityId"] for a in combo["allocations"]] == [env.pids[1]]

    @pytest.mark.parametrize(
        "qs", ["?tier=9", "?tier=abc", "?tier=-1", "?source=0", "?source=x", "?limit=0",
               "?limit=201", "?limit=x", "?offset=-1", "?offset=x"]
    )  # fmt: skip
    async def test_invalid_query_values_are_400(self, env: Any, qs: str) -> None:
        status, _ = await self._get(env, qs)
        assert status == 400

    async def test_never_lists_another_tenants_allocations(self, env: Any) -> None:
        await _pool(env, PRO, 5)
        await _put_tier(env, env.pids[0], PRO)
        c = env.dal.communities
        foreign_id = int(env.dal(c.public_id == env.foreign_pid).select(c.id).first().id)
        await svc.allocate(
            env.adal,
            env.dal,
            tenant_id=env.other,
            community_id=foreign_id,
            tier=PRO,
            actor=None,
            source=svc.SOURCE_OPERATOR_OVERRIDE,
            pool=svc.StorePoolProvider(env.store),
        )
        _, body = await self._get(env)
        assert {a["communityId"] for a in body["allocations"]} == {env.pids[0]}

    async def test_stale_allocation_is_flagged(self, env: Any) -> None:
        await _pool(env, PRO, 5)
        await _put_tier(env, env.pids[0], PRO)
        t = env.dal.community_tier_allocations
        env.dal(t.community_id == env.ids[0]).update(
            last_active_at=svc.now_utc() - timedelta(days=45)
        )
        env.dal.commit()
        _, body = await self._get(env)
        assert body["allocations"][0]["stale"] is True


class TestGuards:
    async def test_flag_off_is_404_on_every_route(self, env: Any, flag: dict[str, bool]) -> None:
        flag["on"] = False
        await _pool(env, PRO, 5)
        calls = [
            await env.client.get(f"{BASE}/license/pool", headers=_h()),
            await env.client.get(f"{BASE}/communities/tier-allocations", headers=_h()),
            await env.client.put(
                f"{BASE}/communities/{env.pids[0]}/tier", headers=_h(), json={"tier": PRO}
            ),
        ]
        assert [c.status_code for c in calls] == [404, 404, 404]
        assert _active(env, env.ids[0]) is None

    async def test_no_token_is_401(self, env: Any) -> None:
        assert (await env.client.get(f"{BASE}/license/pool")).status_code == 401

    @pytest.mark.parametrize(
        "scope",
        [
            "tenant:read",
            "tenant:admin",
            "tenant.community_tier:read",
            "tenant.community_tier:allocate",
        ],
    )
    async def test_allocate_needs_both_tenant_admin_and_allocate_scope(
        self, env: Any, scope: str
    ) -> None:
        await _pool(env, PRO, 5)
        status, _ = await _put_tier(env, env.pids[0], PRO, scope=scope)
        assert status == 403
        assert _active(env, env.ids[0]) is None

    async def test_read_routes_need_tenant_admin_and_read_scope(self, env: Any) -> None:
        for scope in ("tenant:read", "tenant.community_tier:allocate"):
            for path in ("license/pool", "communities/tier-allocations"):
                response = await env.client.get(f"{BASE}/{path}", headers=_h(scope=scope))
                assert response.status_code == 403

    async def test_tenant_mismatch_is_403(self, env: Any) -> None:
        response = await env.client.get(f"/api/v1/tenant/{OTHER_SLUG}/license/pool", headers=_h())
        assert response.status_code == 403


def test_scopes_are_in_the_tenant_admin_bundle_only() -> None:
    from flask_core.auth import SCOPE_BUNDLES

    for scope in ("tenant.community_tier:read", "tenant.community_tier:allocate"):
        assert scope in SCOPE_BUNDLES["tenant"]["admin"]
        assert scope not in SCOPE_BUNDLES["tenant"]["maintainer"]
        assert scope not in SCOPE_BUNDLES["tenant"]["viewer"]


def test_flag_is_client_visible_and_baselined_off() -> None:
    from pathlib import Path

    import yaml

    from blueprints.v1.flags import CLIENT_FLAG_KEYS

    assert "waddles.community-tier-allocation" in CLIENT_FLAG_KEYS
    root = Path(__file__).resolve().parents[2] / "k8s" / "helm" / "waddlebot"
    assert (
        yaml.safe_load((root / "values.yaml").read_text())["config"][
            "communityTierAllocationEnabled"
        ]
        is False
    )
    assert (
        "FLAG_WADDLES_COMMUNITY_TIER_ALLOCATION"
        in (root / "templates" / "configmap.yaml").read_text()
    )
