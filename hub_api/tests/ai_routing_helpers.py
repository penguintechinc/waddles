"""Shared helpers for the AI-routing capability + real-Ollama test modules.

Mirrors the private helpers in `test_ai_routing_router.py` (feature-flag patch,
community seeding, enterprise entitlement) so the newer test modules share one
copy instead of each re-declaring them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from flask_core import feature_flags

from services import community_tier_service as tier_svc
from services import token_ledger
from services.ai_routing import router
from services.schema import bind_community_tier_tables


def patch_feature_flags(
    monkeypatch: pytest.MonkeyPatch, *, disabled: frozenset[str] = frozenset()
) -> None:
    """Stub PostHog (`feature_enabled`): every flag on except those in `disabled`."""

    async def _fake(
        flag_key: str, *, tenant: str, community: int | None = None, default: bool = False
    ) -> bool:
        return flag_key not in disabled

    monkeypatch.setattr(router, "feature_enabled", _fake)


def ensure_tier_tables(dal: Any) -> None:
    """Bind + create the community-tier ledger tables (sqlite needs `migrate=True` + a touch)."""
    bind_community_tier_tables(dal, migrate=True)
    dal.commit()
    dal(dal.community_tier_allocations).count()
    dal(dal.tenant_license_pools).count()


def patch_tenant_tier(monkeypatch: pytest.MonkeyPatch, tier: str | dict[str, str] = "free") -> None:
    """Stub the license gate (`get_tier`): the TENANT tier, which must NOT uplift a community.

    `tier` is a fixed tier name, or a `{tenant_slug: tier}` map (unlisted slugs -> `free`) so a
    test can prove the tenant uplift is bound to the right tenant.
    """

    async def _fake(*, tenant: str, community: int | None = None) -> str:
        return tier if isinstance(tier, str) else tier.get(tenant, "free")

    # The community resolver no longer consults the tenant tier at all (communities are only
    # Free/Premium via an allocation); the stub stays installed on the license-gate entry point so
    # a regression that re-introduced a tenant uplift would be visible to the tests.
    monkeypatch.setattr(feature_flags, "get_tier", _fake)


def seed_community(ai_routing_db: Any) -> tuple[Any, int]:
    """Insert one active Free community (no allocation) under the `acme-corp` tenant."""
    dal = ai_routing_db.dal
    ensure_tier_tables(dal)
    tenant = dal(dal.tenants.slug == "acme-corp").select().first()
    community_id = dal.communities.insert(name="acme", tenant_id=tenant.id, is_active=True)
    dal.commit()
    return ai_routing_db, community_id


async def set_tier(async_dal: Any, community_id: int, tier_code: int) -> None:
    """Give the community an ACTIVE allocation of `tier_code` (the only real tier write path)."""
    grant_tier(async_dal.dal, community_id, tier_code)


def grant_tier(dal: Any, community_id: int, tier_code: int) -> None:
    """Sync form of :func:`set_tier` for fixtures that hold a bare `dal`."""
    ensure_tier_tables(dal)
    now = tier_svc.now_utc()
    tenant_id = dal(dal.communities.id == community_id).select().first().tenant_id
    dal.community_tier_allocations.insert(
        public_id=f"alloc-{community_id}-{tier_code}",
        tenant_id=tenant_id,
        community_id=community_id,
        tier_code=tier_code,
        source_code=tier_svc.SOURCE_OPERATOR_OVERRIDE,
        allocated_at=now,
        last_active_at=now,
        created_at=now,
        updated_at=now,
    )
    dal.commit()


async def set_premium(async_dal: Any, community_id: int) -> None:
    """Allocate the Premium tier to the community (premium AI entitlement half)."""
    await set_tier(async_dal, community_id, tier_svc.TIER_CODE_PREMIUM)


#: Legacy name kept so the many older suites that call it keep working; it is Premium now.
set_enterprise = set_premium


async def credit_premium(
    async_dal: Any, community_id: int, amount: int, *, key: str = "seed"
) -> None:
    """Credit premium-AI tokens through the real ledger."""
    await token_ledger.credit_tokens(
        async_dal,
        async_dal.dal,
        community_id,
        token_ledger.PREMIUM_AI_CONSUMABLE,
        amount,
        idempotency_key=key,
    )


def patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Force every `httpx.AsyncClient` onto an in-memory `MockTransport` (offline unit tests)."""
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def ollama_body(text: str = "ok", **extra: Any) -> dict[str, Any]:
    """A minimal, well-formed non-streaming `/api/generate` response body."""
    body: dict[str, Any] = {
        "response": text,
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 7,
        "eval_count": 3,
    }
    body.update(extra)
    return body
