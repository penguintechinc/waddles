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

from services import token_ledger
from services.ai_routing import router


def patch_feature_flags(
    monkeypatch: pytest.MonkeyPatch, *, disabled: frozenset[str] = frozenset()
) -> None:
    """Stub PostHog (`feature_enabled`): every flag on except those in `disabled`."""

    async def _fake(
        flag_key: str, *, tenant: str, community: int | None = None, default: bool = False
    ) -> bool:
        return flag_key not in disabled

    monkeypatch.setattr(router, "feature_enabled", _fake)


def seed_community(ai_routing_db: Any) -> tuple[Any, int]:
    """Insert one active, un-licensed community under the `acme-corp` tenant."""
    dal = ai_routing_db.dal
    tenant = dal(dal.tenants.slug == "acme-corp").select().first()
    community_id = dal.communities.insert(
        name="acme", tenant_id=tenant.id, is_active=True, license_tier=None
    )
    dal.commit()
    return ai_routing_db, community_id


async def set_enterprise(async_dal: Any, community_id: int) -> None:
    """Flip the community to the Enterprise license tier (premium/BYOK entitlement half)."""
    await async_dal.update_async(
        async_dal.dal.communities.id == community_id, license_tier="enterprise"
    )


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
