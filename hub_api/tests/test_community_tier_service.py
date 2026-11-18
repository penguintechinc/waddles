"""Community tier allocation service on a real (file-backed sqlite) DAL + a real pool store.

The pool limit comes from `StorePoolProvider` over the real `tenant_license_pools` table (not a
mock of the unit under test). The real-Postgres race lives in
`test_community_tier_service_pg.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from services import community_tier_service as svc
from services.audit_events import AuditOutcome
from services.schema import bind_community_tier_tables
from tests.conftest import TENANT_SLUG

PRO, FREE = svc.TIER_CODE_PREMIUM, svc.TIER_CODE_FREE  # PRO = the one pooled tier (Premium)


class RecordingAudit:
    """Real in-memory audit sink with the `AuditService.record` surface."""

    def __init__(self) -> None:
        """Start empty."""
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def env(admin_db: Any) -> Iterator[Any]:
    dal = admin_db.dal
    bind_community_tier_tables(dal, migrate=True)
    tenant_id = int(dal(dal.tenants.slug == TENANT_SLUG).select().first().id)
    other_tenant = int(dal.tenants.insert(slug="other-co", display_name="Other", is_active=True))
    communities = [
        int(dal.communities.insert(name=f"c{i}", tenant_id=tenant_id, is_premium=False))
        for i in range(5)
    ]
    foreign = int(dal.communities.insert(name="foreign", tenant_id=other_tenant))
    dal.commit()
    for name in ("community_tier_allocations", "tenant_license_pools"):
        dal(dal[name]).count()
    store = svc.PoolStore(admin_db, dal)
    yield type(
        "Env",
        (),
        {
            "adal": admin_db,
            "dal": dal,
            "tenant": tenant_id,
            "other": other_tenant,
            "cs": communities,
            "foreign": foreign,
            "store": store,
            "pool": svc.StorePoolProvider(store),
            "audit": RecordingAudit(),
        },
    )
    svc._touch_seen.clear()


async def _set_pool(env: Any, tier: int, limit: int | None) -> None:
    await env.store.put(env.tenant, tier, limit, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc())


async def _alloc(env: Any, community: int, tier: int, **kw: Any) -> svc.AllocationResult:
    kw.setdefault("source", svc.SOURCE_TENANT_ADMIN)
    kw.setdefault("pool", env.pool)
    kw.setdefault("audit", env.audit)
    return await svc.allocate(
        env.adal,
        env.dal,
        tenant_id=env.tenant,
        community_id=community,
        tier=tier,
        actor=None,
        **kw,
    )


def _community(env: Any, cid: int) -> Any:
    return env.dal(env.dal.communities.id == cid).select().first()


def _active(env: Any, cid: int) -> Any:
    t = env.dal.community_tier_allocations
    return env.dal((t.community_id == cid) & (t.released_at == None)).select().first()  # noqa: E711


def _assert_consistent(env: Any, cid: int) -> None:
    row, alloc = _community(env, cid), _active(env, cid)
    tier = int(alloc.tier_code) if alloc else FREE
    assert (row.license_tier or "free") == svc.TIER_NAMES[tier]
    assert bool(row.is_premium) is (tier == PRO)


async def test_allocate_within_pool_updates_denormalized_columns(env: Any) -> None:
    await _set_pool(env, PRO, 2)
    res = await _alloc(env, env.cs[0], PRO)
    assert res.changed and res.used == 1 and res.limit == 2 and res.overage == 0
    _assert_consistent(env, env.cs[0])
    assert env.audit.events[-1].action == "license.community_tier_allocated"


async def test_pool_exhausted_blocks_new_and_audits(env: Any) -> None:
    await _set_pool(env, PRO, 1)
    await _alloc(env, env.cs[0], PRO)
    with pytest.raises(svc.PoolExhausted) as exc:
        await _alloc(env, env.cs[1], PRO)
    assert (exc.value.required_tier, exc.value.limit, exc.value.used) == (PRO, 1, 1)
    assert _active(env, env.cs[1]) is None
    _assert_consistent(env, env.cs[1])
    blocked = env.audit.events[-1]
    assert blocked.action == "license.community_tier_allocation_blocked"
    assert blocked.outcome is AuditOutcome.DENIED


async def test_concurrent_allocators_for_last_slot_exactly_one_wins(env: Any) -> None:
    await _set_pool(env, PRO, 1)
    results = await asyncio.gather(*[_alloc(env, c, PRO) for c in env.cs], return_exceptions=True)
    wins = [r for r in results if isinstance(r, svc.AllocationResult)]
    losses = [r for r in results if isinstance(r, svc.PoolExhausted)]
    assert len(wins) == 1 and len(losses) == len(env.cs) - 1


async def test_shrinking_pool_keeps_rows_and_only_blocks_new(env: Any) -> None:
    await _set_pool(env, PRO, 3)
    for c in env.cs[:3]:
        await _alloc(env, c, PRO)
    await _set_pool(env, PRO, 1)  # downgrade: over by 2
    usage = (await svc.pool_usage(env.adal, env.dal, env.tenant, pool=_both(env)))[PRO]
    assert (usage.used, usage.limit, usage.overage, usage.available) == (3, 1, 2, 0)
    for c in env.cs[:3]:
        assert _active(env, c) is not None  # never stripped
        _assert_consistent(env, c)
    with pytest.raises(svc.PoolExhausted):
        await _alloc(env, env.cs[3], PRO)
    # an already-provisioned community re-allocating the same tier is a no-op, not blocked
    again = await _alloc(env, env.cs[0], PRO)
    assert not again.changed


def _both(env: Any) -> svc.StorePoolProvider:
    return env.pool


async def _age(env: Any, community: int, days: int) -> None:
    """Backdate one allocation's `last_active_at` (days past now)."""
    t = env.dal.community_tier_allocations
    env.dal(t.community_id == community).update(last_active_at=svc.now_utc() - timedelta(days=days))
    env.dal.commit()


async def test_stale_rows_keep_their_slot_and_tier_until_released_and_touch_clears_flag(
    env: Any,
) -> None:
    """Contract change (staleness-pool-cap-defeat fix): stale rows still COUNT toward the pool.

    Previously stale rows were excluded from `used`, so a tenant at its limit could re-allocate a
    whole new pool every 30 days; no activity signal feeds `touch_active`, so reclamation is off.
    """
    await _set_pool(env, PRO, 2)
    await _alloc(env, env.cs[0], PRO)
    await _alloc(env, env.cs[1], PRO)
    for c in env.cs[:2]:
        await _age(env, c, svc.STALE_AFTER.days + 1)  # 31 days past last_active_at

    # (3) usage still reports used == limit and still reports the stale count
    usage = (await svc.pool_usage(env.adal, env.dal, env.tenant, pool=env.pool))[PRO]
    assert (usage.used, usage.limit, usage.available, usage.overage) == (2, 2, 0, 0)
    assert usage.stale_excluded == 2

    # (1) a NEW allocation for another community is blocked
    with pytest.raises(svc.PoolExhausted) as exc:
        await _alloc(env, env.cs[2], PRO)
    assert (exc.value.limit, exc.value.used) == (2, 2)
    assert _active(env, env.cs[2]) is None
    assert env.audit.events[-1].action == "license.community_tier_allocation_blocked"

    # (2) the old allocations still confer their tier (never stripped)
    for c in env.cs[:2]:
        assert _community(env, c).license_tier == "premium"
        assert await svc.resolve_effective_tier(env.adal, env.dal, env.tenant, c) == "premium"

    # (4) releasing one frees exactly one slot; the next allocation succeeds, a further one fails
    await _alloc(env, env.cs[0], FREE)
    usage = (await svc.pool_usage(env.adal, env.dal, env.tenant, pool=env.pool))[PRO]
    assert (usage.used, usage.available, usage.stale_excluded) == (1, 1, 1)
    await _alloc(env, env.cs[2], PRO)
    with pytest.raises(svc.PoolExhausted):
        await _alloc(env, env.cs[3], PRO)

    # (5) touch_active refreshes last_active_at and clears the stale flag; count is unchanged
    before = (await svc.pool_usage(env.adal, env.dal, env.tenant, pool=env.pool))[PRO]
    assert (before.used, before.stale_excluded) == (2, 1)
    assert await svc.touch_active(env.adal, env.dal, env.cs[1]) is True
    row = _active(env, env.cs[1])
    assert not svc._is_stale(row, svc.now_utc())  # noqa: SLF001
    after = (await svc.pool_usage(env.adal, env.dal, env.tenant, pool=env.pool))[PRO]
    assert (after.used, after.stale_excluded) == (2, 0)


async def test_touch_active_rate_limited(env: Any) -> None:
    await _set_pool(env, PRO, 1)
    await _alloc(env, env.cs[0], PRO)
    assert await svc.touch_active(env.adal, env.dal, env.cs[0]) is False  # fresh row
    assert await svc.touch_active(env.adal, env.dal, env.cs[4]) is False  # no allocation


async def test_free_never_blocked_and_releases_slot(env: Any) -> None:
    await _set_pool(env, PRO, 1)
    await _alloc(env, env.cs[0], PRO)
    freed = await _alloc(env, env.cs[0], FREE)
    assert freed.changed and _active(env, env.cs[0]) is None
    _assert_consistent(env, env.cs[0])
    assert env.audit.events[-1].action == "license.community_tier_released"
    await _alloc(env, env.cs[1], PRO)  # slot reusable
    # Free with no pool at all (never seen) is never blocked, even unlicensed
    noop = await _alloc(env, env.cs[2], FREE)
    assert not noop.changed
    t = env.dal.community_tier_allocations
    released = env.dal(t.community_id == env.cs[0]).select().first()
    assert released.release_reason_code == svc.RELEASE_DEALLOC


async def test_pool_usage_has_exactly_one_premium_entry(env: Any) -> None:
    await _set_pool(env, PRO, 2)
    await _alloc(env, env.cs[0], PRO)
    usage = await svc.pool_usage(env.adal, env.dal, env.tenant, pool=env.pool)
    assert list(usage) == [PRO] and svc.POOLED_TIER_CODES == (PRO,)
    assert usage[PRO].used == 1


async def test_only_tiers_free_and_premium_exist(env: Any) -> None:
    assert svc.TIER_NAMES == {1: "free", 2: "premium"}
    assert not hasattr(svc, "TIER_CODE_PROFESSIONAL") and not hasattr(svc, "TIER_CODE_ENTERPRISE")
    await _set_pool(env, PRO, 5)
    with pytest.raises(ValueError):
        await _alloc(env, env.cs[0], 3)  # the removed Enterprise code is rejected
    with pytest.raises(ValueError):
        await env.store.put(env.tenant, 3, 1, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc())


async def test_allocate_within_pool_then_blocked_beyond_with_required_tier_premium(
    env: Any,
) -> None:
    await _set_pool(env, PRO, 1)
    ok = await _alloc(env, env.cs[0], PRO)
    assert ok.changed and (ok.used, ok.limit) == (1, 1)
    _assert_consistent(env, env.cs[0])
    assert _community(env, env.cs[0]).license_tier == "premium"
    with pytest.raises(svc.PoolExhausted) as exc:
        await _alloc(env, env.cs[1], PRO)
    assert exc.value.required_tier == 2


async def test_operator_override_never_blocked_but_counted_and_audited(env: Any) -> None:
    await _set_pool(env, PRO, 1)
    await _alloc(env, env.cs[0], PRO)
    res = await _alloc(env, env.cs[1], PRO, source=svc.SOURCE_OPERATOR_OVERRIDE)
    assert res.changed and res.overage == 1 and res.used == 2
    assert env.audit.events[-1].action == "license.community_tier_overridden"
    usage = (await svc.pool_usage(env.adal, env.dal, env.tenant, pool=env.pool))[PRO]
    assert (usage.used, usage.overage) == (2, 1)
    _assert_consistent(env, env.cs[1])


async def test_operator_override_proceeds_without_known_pool(env: Any) -> None:
    res = await _alloc(env, env.cs[0], PRO, source=svc.SOURCE_OPERATOR_OVERRIDE)
    assert res.changed and res.limit is None


async def test_pool_unavailable_propagates_for_tenant_admin(env: Any) -> None:
    with pytest.raises(svc.PoolUnavailable):
        await _alloc(env, env.cs[0], PRO)  # never seen: no silent zero
    assert _active(env, env.cs[0]) is None
    with pytest.raises(svc.PoolUnavailable):
        await svc.pool_usage(env.adal, env.dal, env.tenant, pool=env.pool)


async def test_not_licensed_pool_blocks_with_zero_limit(env: Any) -> None:
    await _set_pool(env, PRO, None)
    with pytest.raises(svc.PoolExhausted) as exc:
        await _alloc(env, env.cs[0], PRO)
    assert exc.value.limit == 0


async def test_unlimited_pool_never_blocks(env: Any) -> None:
    await _set_pool(env, PRO, svc.UNLIMITED)
    for c in env.cs:
        await _alloc(env, c, PRO)
    usage = (await svc.pool_usage(env.adal, env.dal, env.tenant, pool=_both_pro(env)))[PRO]
    assert usage.unlimited and usage.available is None and usage.overage == 0


def _both_pro(env: Any) -> Any:
    class _P:
        async def get_pool(self, tenant_id: int, tier_code: int) -> svc.PoolInfo:
            return await env.pool.get_pool(tenant_id, PRO)

    return _P()


async def test_foreign_community_rejected_numeric_keys_only(env: Any) -> None:
    await _set_pool(env, PRO, 5)
    with pytest.raises(svc.CommunityNotInTenant):
        await _alloc(env, env.foreign, PRO)
    with pytest.raises(svc.CommunityNotInTenant):
        await _alloc(env, 99999, PRO)
    with pytest.raises(ValueError):
        await _alloc(env, env.cs[0], 7)
    with pytest.raises(ValueError):
        await _alloc(env, env.cs[0], PRO, reason="free text with spaces")


async def test_effective_tier_premium_iff_active_allocation_and_fails_closed(env: Any) -> None:
    await _set_pool(env, PRO, 2)
    await _alloc(env, env.cs[0], PRO)
    await _alloc(env, env.cs[2], PRO, source=svc.SOURCE_OPERATOR_OVERRIDE)
    a, d, t = env.adal, env.dal, env.tenant
    assert await svc.resolve_effective_tier(a, d, t, env.cs[0]) == "premium"
    assert await svc.resolve_effective_tier(a, d, t, env.cs[2]) == "premium"  # operator override
    assert await svc.resolve_effective_tier(a, d, t, env.cs[1]) == "free"  # no allocation
    # (f) the tenant's own license tier never uplifts a community: no tenant-tier input exists
    import inspect

    assert "tenant_tier" not in inspect.signature(svc.resolve_effective_tier).parameters
    # a community of another tenant never confers its tier here
    assert await svc.resolve_effective_tier(a, d, env.other, env.cs[0]) == "free"
    # a released allocation drops back to free
    await _alloc(env, env.cs[0], FREE)
    assert await svc.resolve_effective_tier(a, d, t, env.cs[0]) == "free"


async def test_effective_tier_counts_backfill_rows_and_fails_closed_on_error(
    env: Any, caplog: Any
) -> None:
    await _set_pool(env, PRO, 5)
    await _alloc(env, env.cs[3], PRO, source=svc.SOURCE_BACKFILL)
    assert await svc.resolve_effective_tier(env.adal, env.dal, env.tenant, env.cs[3]) == "premium"

    class _BrokenDal:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("db down")

    with caplog.at_level("ERROR"):
        assert await svc.resolve_effective_tier(env.adal, _BrokenDal(), env.tenant, env.cs[3]) == (
            "free"
        )
    assert "community_tier_resolve_failed error_type=RuntimeError" in caplog.text


async def test_pool_store_upsert_roundtrip(env: Any) -> None:
    assert await env.store.get(env.tenant, PRO) is None
    await _set_pool(env, PRO, 4)
    await _set_pool(env, PRO, 7)
    row = await env.store.get(env.tenant, PRO)
    assert row is not None and row.limit_count == 7
    info = await env.pool.get_pool(env.tenant, PRO)
    assert info.limit == 7 and info.source == "operator_set" and not info.stale
    with pytest.raises(ValueError):
        await env.store.put(env.tenant, FREE, 1, 3, svc.now_utc())


class _FixedPool:
    """Provider returning a fixed (possibly stale) answer, to model a lookup made pre-lock."""

    def __init__(self, limit: int | None, source: str = "operator_set") -> None:
        self.limit, self.source = limit, source

    async def get_pool(self, tenant_id: int, tier_code: int) -> svc.PoolInfo:
        return svc.PoolInfo(self.limit, svc.now_utc(), False, self.source)  # type: ignore[arg-type]


class _DownPool:
    async def get_pool(self, tenant_id: int, tier_code: int) -> svc.PoolInfo:
        raise svc.PoolUnavailable("down")


async def test_pool_shrink_after_prefetch_is_honored_under_the_lock(env: Any) -> None:
    """The provider answered 5 pre-lock, but the stored pool is now 1: the fresh row wins."""
    await _set_pool(env, PRO, 1)
    await _alloc(env, env.cs[0], PRO)
    with pytest.raises(svc.PoolExhausted) as exc:
        await _alloc(env, env.cs[1], PRO, pool=_FixedPool(5))  # stale prefetch says 5
    assert (exc.value.limit, exc.value.used) == (1, 1)
    assert _active(env, env.cs[1]) is None


async def test_idempotent_reput_succeeds_when_pool_source_is_down(env: Any) -> None:
    await _set_pool(env, PRO, 1)
    await _alloc(env, env.cs[0], PRO)
    res = await _alloc(env, env.cs[0], PRO, pool=_DownPool())
    assert res.changed is False and res.tier_code == PRO
    _assert_consistent(env, env.cs[0])


async def test_new_slot_without_a_known_pool_is_unavailable_not_free(env: Any) -> None:
    with pytest.raises(svc.PoolUnavailable):
        await _alloc(env, env.cs[2], PRO, pool=_DownPool())
    assert _active(env, env.cs[2]) is None
    # release (Free) never needs the pool
    res = await _alloc(env, env.cs[2], FREE, pool=_DownPool())
    assert res.tier_code == FREE


async def test_operator_override_proceeds_without_a_known_pool(env: Any) -> None:
    res = await _alloc(env, env.cs[3], PRO, pool=_DownPool(), source=svc.SOURCE_OPERATOR_OVERRIDE)
    assert res.changed and res.overage >= 1


async def test_release_refreshes_the_utilization_gauge(env: Any) -> None:
    from services import community_tier_telemetry as tel

    await _set_pool(env, PRO, 2)
    await _alloc(env, env.cs[0], PRO)
    await _alloc(env, env.cs[1], PRO)
    assert tel._state[(env.tenant, "premium")] == (2, 2)  # noqa: SLF001
    await _alloc(env, env.cs[0], FREE)
    assert tel._state[(env.tenant, "premium")] == (1, 2)  # noqa: SLF001


async def test_audit_failure_is_loud_retried_and_counted(env: Any, caplog: Any) -> None:
    class Flaky:
        calls = 0

        async def record(self, event: Any) -> None:
            Flaky.calls += 1
            raise RuntimeError("audit down")

    await _set_pool(env, PRO, 2)
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        await _alloc(env, env.cs[0], PRO, audit=Flaky())
    assert Flaky.calls == 2  # one retry, then loud
    assert "community_tier_audit_write_failed" in caplog.text
    assert _active(env, env.cs[0]) is not None  # change committed; failure NOT faked as success


async def test_pool_store_put_is_an_upsert_and_evicts_only_license_server_rows(
    env: Any,
) -> None:
    await env.store.put(env.tenant, PRO, 1, svc.POOL_SOURCE_LICENSE_SERVER, svc.now_utc())
    await env.store.put(env.tenant, PRO, 9, svc.POOL_SOURCE_LICENSE_SERVER, svc.now_utc())
    t = env.dal.tenant_license_pools
    assert env.dal(t.tenant_id == env.tenant).count() == 1
    assert (await env.store.get(env.tenant, PRO)).limit_count == 9
    await env.store.put(env.other, PRO, 4, svc.POOL_SOURCE_OPERATOR_SET, svc.now_utc())
    assert await env.store.delete_license_server_rows(env.tenant) == 1
    assert await env.store.get(env.tenant, PRO) is None
    assert (await env.store.get(env.other, PRO)).limit_count == 4  # operator rows untouched


async def test_pool_usage_counts_in_sql_and_reports_stale_but_includes_it(env: Any) -> None:
    await _set_pool(env, PRO, 3)
    await _alloc(env, env.cs[0], PRO)
    await _alloc(env, env.cs[1], PRO)
    t = env.dal.community_tier_allocations
    env.dal(t.community_id == env.cs[1]).update(last_active_at=svc.now_utc() - timedelta(days=45))
    env.dal.commit()
    usage = await svc.pool_usage(env.adal, env.dal, env.tenant, pool=env.pool)
    # contract change: stale rows are reported AND still counted in `used`
    assert (usage[PRO].used, usage[PRO].stale_excluded, usage[PRO].available) == (2, 1, 1)
