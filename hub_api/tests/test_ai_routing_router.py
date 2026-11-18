"""`services/ai_routing/router.py::route_completion` -- tier selection, fallback, metering.

`flask_core.feature_flags.feature_enabled` (PostHog itself) and the model
HTTP clients (`OllamaClient.generate`/`byok_client_for`) are monkeypatched
-- both have their own real-behavior coverage elsewhere (`test_ai_routing_
clients.py`, flask_core's own test suite). Everything else here is real:
entitlement's license-tier half (the single resolver over a real
`community_tier_allocations` row), the community AI config (`config_service`, real pydal
writes), and
the token ledger (`services.token_ledger`, which delegates to `services.
token_billing_service`'s real, atomic `community_token_balances`/
`token_transactions` ledger, migration 076, against `ai_routing_db`) -- so
"metering debit called on premium" and "block vs fallback on insufficient
balance" are both exercised end-to-end, not asserted against a mock call.
`test_premium_succeeds_and_meters_when_affordable`/`test_premium_ambient_
falls_back_to_free_when_unaffordable` additionally assert directly against
`token_billing_service`'s own tables (not just `token_ledger`'s balance
abstraction) -- proof the real ledger, not a parallel one, is what's
written.

Fail-first proof (executed, not narrated): temporarily changed the
`if balance > 0:` pre-check in `route_completion` to `if balance >= 0:`
(always true) -- `test_premium_ambient_falls_back_to_free_when_unaffordable`
went red (premium would have been attempted with a zero balance instead of
falling back); reverted, confirmed green again.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from services import community_tier_service as tier_svc
from services import token_billing_service, token_ledger
from services.ai_routing import config_service, router
from services.ai_routing.errors import ApiError
from services.ai_routing.models import AIRequest, AIResponse
from tests.ai_routing_helpers import patch_tenant_tier, seed_community, set_enterprise, set_tier

FREE_CODE = tier_svc.TIER_CODE_FREE
PRO_CODE = tier_svc.TIER_CODE_PROFESSIONAL
ENT_CODE = tier_svc.TIER_CODE_ENTERPRISE


def _patch_feature_flags(
    monkeypatch: pytest.MonkeyPatch, *, disabled: frozenset[str] = frozenset()
) -> None:
    async def _fake(
        flag_key: str, *, tenant: str, community: int | None = None, default: bool = False
    ) -> bool:
        return flag_key not in disabled

    monkeypatch.setattr(router, "feature_enabled", _fake)


def _patch_free_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_generate(self: Any, ai_request: AIRequest, *, tier: str) -> AIResponse:
        return AIResponse(
            text=f"free response to: {ai_request.prompt}",
            provider="ollama",
            model="llama3.1:1b",
            tier_used=tier,  # type: ignore[arg-type]
            input_tokens=10,
            output_tokens=5,
        )

    monkeypatch.setattr(router.OllamaClient, "generate", _fake_generate)


def _patch_byok(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: AIResponse | None = None,
    raises: ApiError | None = None,
) -> None:
    class _FakeByokClient:
        async def generate(self, api_key: str, ai_request: AIRequest) -> AIResponse:
            if raises is not None:
                raise raises
            assert response is not None
            return response

    monkeypatch.setattr(router, "byok_client_for", lambda provider: _FakeByokClient())


@pytest.fixture(autouse=True)
def _free_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_tenant_tier(monkeypatch, "free")


@pytest.fixture
def router_db(ai_routing_db: Any) -> Any:
    return seed_community(ai_routing_db)


async def _set_enterprise(async_dal: Any, community_id: int) -> None:
    await set_enterprise(async_dal, community_id)


class TestBaseGate:
    async def test_disabled_flag_raises_ai_routing_disabled(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        _patch_feature_flags(monkeypatch, disabled=frozenset({router.FEATURE_AI_ROUTING}))
        with pytest.raises(ApiError) as exc_info:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                tenant="acme-corp",
                community_id=community_id,
                actor_user_id=1,
                ai_request=AIRequest(prompt="hi"),
                idempotency_key="k1",
            )
        assert exc_info.value.status_code == 503


class TestAiEnabledKillSwitch:
    """`route_completion(..., ai_enabled=...)` -- the deploy-time `WADDLES_AI_ENABLED` gate.

    Distinct from `TestBaseGate` above (the per-community PostHog flag):
    this is the whole-deployment ops switch (`config.py`'s `HubAPIConfig.
    ai_enabled`, threaded in by `blueprints/v1/ai_routing.py`). Proves the
    guard is `route_completion()`'s own first line, independent of the
    blueprint -- unset/default keeps the normal flag+entitlement gating
    path reachable; `ai_enabled=False` raises before that path, before any
    DAL/config_service call, and before any model client is ever touched.
    """

    async def test_default_unset_reaches_normal_flag_gate(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Omitting `ai_enabled` (the real call shape every pre-existing test above uses)."""
        async_dal, community_id = router_db
        _patch_feature_flags(monkeypatch)
        _patch_free_ollama(monkeypatch)
        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi"),
            idempotency_key="k1",
        )
        assert response.tier_used == "free"

    async def test_ai_enabled_false_raises_before_flag_check(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db

        async def _fail_if_called(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("feature_enabled() must never be called when ai_enabled=False")

        monkeypatch.setattr(router, "feature_enabled", _fail_if_called)

        with pytest.raises(ApiError) as exc_info:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                tenant="acme-corp",
                community_id=community_id,
                actor_user_id=1,
                ai_request=AIRequest(prompt="hi"),
                idempotency_key="k1",
                ai_enabled=False,
            )
        assert exc_info.value.status_code == 503
        assert exc_info.value.code == "AI_DISABLED_DEPLOYMENT"

    async def test_ai_enabled_false_never_calls_the_model_client(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mock the model client and assert it is never invoked -- no outbound call attempted."""
        async_dal, community_id = router_db
        _patch_feature_flags(monkeypatch)  # would allow every gate if reached -- proves it isn't

        called = False

        async def _fail_if_called(self: Any, ai_request: AIRequest, *, tier: str) -> AIResponse:
            nonlocal called
            called = True
            raise AssertionError("OllamaClient.generate() must never be called")

        monkeypatch.setattr(router.OllamaClient, "generate", _fail_if_called)

        with pytest.raises(ApiError) as exc_info:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                tenant="acme-corp",
                community_id=community_id,
                actor_user_id=1,
                ai_request=AIRequest(prompt="hi", requested_tier="premium"),
                idempotency_key="k1",
                ai_enabled=False,
            )
        assert exc_info.value.code == "AI_DISABLED_DEPLOYMENT"
        assert called is False


class TestFreeTier:
    async def test_default_config_routes_to_free(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        _patch_feature_flags(monkeypatch)
        _patch_free_ollama(monkeypatch)
        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi"),
            idempotency_key="k1",
        )
        assert response.tier_used == "free"
        assert response.billed_tokens == 0
        assert response.fallback_reason is None


class TestPremiumTier:
    async def test_premium_succeeds_and_meters_when_affordable(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        await _set_enterprise(async_dal, community_id)
        await token_ledger.credit_tokens(
            async_dal,
            async_dal.dal,
            community_id,
            token_ledger.PREMIUM_AI_CONSUMABLE,
            1000,
            idempotency_key="seed",
        )
        _patch_feature_flags(monkeypatch)

        async def _fake_premium_generate(
            self: Any, ai_request: AIRequest, *, tier: str
        ) -> AIResponse:
            return AIResponse(
                text="premium answer",
                provider="ollama",
                model="gemma2:27b",
                tier_used=tier,
                input_tokens=100,
                output_tokens=50,  # type: ignore[arg-type]
            )

        monkeypatch.setattr(router.OllamaClient, "generate", _fake_premium_generate)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=7,
            ai_request=AIRequest(prompt="hi", requested_tier="premium"),
            idempotency_key="debit-1",
        )
        assert response.tier_used == "premium"
        assert response.billed_tokens == 150
        assert response.fallback_reason is None

        balance = await token_ledger.get_balance(
            async_dal, async_dal.dal, community_id=community_id
        )
        assert balance == 850  # 1000 - (100 + 50), real metering debit

        # Prove it's the REAL ledger (`token_billing_service`, migration
        # 076's `community_token_balances`/`token_transactions`) being
        # written, not just `token_ledger`'s own abstraction agreeing with
        # itself -- read both tables directly.
        real_balances = await token_billing_service.list_balances(
            async_dal, async_dal.dal, community_id=community_id
        )
        ai_balance = next(b for b in real_balances if b.product_key == "ai_routing_call")
        assert ai_balance.balance == 850

        real_transactions, total = await token_billing_service.list_transactions(
            async_dal, async_dal.dal, community_id=community_id, product_key="ai_routing_call"
        )
        assert total == 2  # the seed credit (+1000) and this debit (-150)
        debit_row = next(t for t in real_transactions if t.delta < 0)
        assert debit_row.delta == -150
        assert debit_row.balance_after == 850
        assert debit_row.ref == "debit-1"  # idempotency_key stored as the real ledger's `ref`

    async def test_premium_ambient_falls_back_to_free_when_unaffordable(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        await _set_enterprise(async_dal, community_id)
        # No balance credited -- 0.
        _patch_feature_flags(monkeypatch)
        _patch_free_ollama(monkeypatch)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi", requested_tier="premium", invocation="ambient"),
            idempotency_key="k1",
        )
        assert response.tier_used == "free"
        assert response.fallback_reason == "insufficient_balance"
        assert response.billed_tokens == 0

        # No real debit occurred -- the pre-check (`token_ledger.get_
        # balance()`) blocked the premium path before `_run_premium()`
        # (and therefore `token_billing_service.debit_tokens()`) was ever
        # called, so the real ledger has zero transaction rows for this
        # community.
        _real_transactions, total = await token_billing_service.list_transactions(
            async_dal, async_dal.dal, community_id=community_id, product_key="ai_routing_call"
        )
        assert total == 0

    async def test_premium_interactive_blocks_when_policy_is_block(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        await _set_enterprise(async_dal, community_id)
        await config_service.set_ai_config(
            async_dal,
            async_dal.dal,
            community_id=community_id,
            preferred_tier="premium",
            byok_provider=None,
            on_insufficient_balance="block",
            updated_by_user_id=1,
        )
        _patch_feature_flags(monkeypatch)

        with pytest.raises(ApiError) as exc_info:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                tenant="acme-corp",
                community_id=community_id,
                actor_user_id=1,
                ai_request=AIRequest(prompt="hi", invocation="interactive"),
                idempotency_key="k1",
            )
        assert exc_info.value.status_code == 402
        assert exc_info.value.code == "AI_INSUFFICIENT_BALANCE"

    async def test_premium_interactive_not_entitled_raises_403(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        # license_tier left None -- never upgraded to enterprise.
        _patch_feature_flags(monkeypatch)

        with pytest.raises(ApiError) as exc_info:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                tenant="acme-corp",
                community_id=community_id,
                actor_user_id=1,
                ai_request=AIRequest(
                    prompt="hi", requested_tier="premium", invocation="interactive"
                ),
                idempotency_key="k1",
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.code == "AI_TIER_NOT_ENTITLED"

    async def test_premium_ambient_not_entitled_falls_back_to_free(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        _patch_feature_flags(monkeypatch)
        _patch_free_ollama(monkeypatch)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi", requested_tier="premium", invocation="ambient"),
            idempotency_key="k1",
        )
        assert response.tier_used == "free"
        assert response.fallback_reason == "not_entitled"


class TestByokTier:
    async def test_byok_success_when_entitled_and_key_configured(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        await _set_enterprise(async_dal, community_id)
        monkeypatch.setenv("AI_BYOK_ENCRYPTION_KEY", "22" * 32)

        async def _ok_validate(provider: str, api_key: str, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(config_service, "validate_byok_key", _ok_validate)
        await config_service.set_byok_key(
            async_dal,
            async_dal.dal,
            community_id=community_id,
            provider="openai",
            plaintext_key="sk-real-community-key",
            created_by_user_id=1,
        )
        _patch_feature_flags(monkeypatch)
        expected = AIResponse(
            text="byok answer",
            provider="openai",
            model="gpt-4o-mini",
            tier_used="byok",
            input_tokens=20,
            output_tokens=10,
        )
        _patch_byok(monkeypatch, response=expected)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi", requested_tier="byok", byok_provider="openai"),
            idempotency_key="k1",
        )
        assert response.tier_used == "byok"
        assert response.billed_tokens == 0  # BYOK never charges platform tokens
        assert response.text == "byok answer"

    async def test_byok_ambient_falls_back_to_free_when_key_missing(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        await _set_enterprise(async_dal, community_id)
        await config_service.set_ai_config(
            async_dal,
            async_dal.dal,
            community_id=community_id,
            preferred_tier="byok",
            byok_provider="openai",
            on_insufficient_balance="fallback_free",
            updated_by_user_id=1,
        )
        _patch_feature_flags(monkeypatch)
        _patch_free_ollama(monkeypatch)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi", invocation="ambient"),
            idempotency_key="k1",
        )
        assert response.tier_used == "free"
        # Provider IS configured (openai) but no key is on file -- `_run_byok`
        # raises `byok_key_missing`, caught by the ambient-fallback handler.
        assert response.fallback_reason == "byok_call_failed"

    async def test_byok_ambient_falls_back_to_free_when_no_provider_configured(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        await _set_enterprise(async_dal, community_id)
        # `ai_model_config` never set -- `byok_provider` defaults to `None`.
        _patch_feature_flags(monkeypatch)
        _patch_free_ollama(monkeypatch)

        response = await router.route_completion(
            async_dal,
            async_dal.dal,
            tenant="acme-corp",
            community_id=community_id,
            actor_user_id=1,
            ai_request=AIRequest(prompt="hi", requested_tier="byok", invocation="ambient"),
            idempotency_key="k1",
        )
        assert response.tier_used == "free"
        assert response.fallback_reason == "byok_provider_not_configured"

    async def test_byok_interactive_key_missing_raises_409(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async_dal, community_id = router_db
        await _set_enterprise(async_dal, community_id)
        _patch_feature_flags(monkeypatch)

        with pytest.raises(ApiError) as exc_info:
            await router.route_completion(
                async_dal,
                async_dal.dal,
                tenant="acme-corp",
                community_id=community_id,
                actor_user_id=1,
                ai_request=AIRequest(
                    prompt="hi",
                    requested_tier="byok",
                    byok_provider="openai",
                    invocation="interactive",
                ),
                idempotency_key="k1",
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "AI_BYOK_KEY_MISSING"


class TestByokProfessionalFloor:
    """BYOK entitlement floor is Professional (2026-11-06 decision), premium stays Enterprise.

    Every case drives the single resolver (`resolve_effective_tier`) over real allocation rows and
    a stubbed tenant-tier license gate.
    """

    @pytest.mark.parametrize(
        ("tier_code", "byok", "premium"),
        [
            (None, False, False),
            (FREE_CODE, False, False),
            (PRO_CODE, True, False),
            (ENT_CODE, True, True),
        ],
    )
    async def test_allocation_floor(
        self, router_db: Any, tier_code: int | None, byok: bool, premium: bool
    ) -> None:
        async_dal, community_id = router_db
        if tier_code is not None and tier_code != FREE_CODE:
            await set_tier(async_dal, community_id, tier_code)
        assert (
            await router._is_professional_or_higher(
                async_dal, async_dal.dal, community_id=community_id
            )
            is byok
        )
        assert (
            await router._is_enterprise_tier(async_dal, async_dal.dal, community_id=community_id)
            is premium
        )

    @pytest.mark.parametrize(
        ("tenant_tier", "byok", "premium"),
        [("free", False, False), ("professional", True, False), ("enterprise", True, True)],
    )
    async def test_tenant_tier_uplift_honored(
        self,
        router_db: Any,
        monkeypatch: pytest.MonkeyPatch,
        tenant_tier: str,
        byok: bool,
        premium: bool,
    ) -> None:
        """Tenant tier X, community Free (no allocation) -> entitled per X."""
        async_dal, community_id = router_db
        patch_tenant_tier(monkeypatch, {"acme-corp": tenant_tier})
        dal = async_dal.dal
        assert (
            await router._is_professional_or_higher(async_dal, dal, community_id=community_id)
            is byok
        )
        assert (
            await router._is_enterprise_tier(async_dal, dal, community_id=community_id) is premium
        )

    async def test_free_tenant_pro_allocation_is_byok_not_premium(self, router_db: Any) -> None:
        async_dal, community_id = router_db
        await set_tier(async_dal, community_id, PRO_CODE)
        dal = async_dal.dal
        assert await router._is_professional_or_higher(async_dal, dal, community_id=community_id)
        assert not await router._is_enterprise_tier(async_dal, dal, community_id=community_id)

    async def test_inactive_community_resolves_free_even_with_allocation(
        self, router_db: Any
    ) -> None:
        async_dal, community_id = router_db
        await set_tier(async_dal, community_id, ENT_CODE)
        dal = async_dal.dal
        assert await router._is_enterprise_tier(async_dal, dal, community_id=community_id)
        await async_dal.update_async(dal.communities.id == community_id, is_active=False)
        assert not await router._is_enterprise_tier(async_dal, dal, community_id=community_id)
        assert not await router._is_professional_or_higher(
            async_dal, dal, community_id=community_id
        )

    async def test_other_tenant_slug_gets_the_same_denial_as_free(self, router_db: Any) -> None:
        """A caller of tenant B naming tenant A's Enterprise community gets Free (no oracle)."""
        async_dal, community_id = router_db
        await set_tier(async_dal, community_id, ENT_CODE)
        dal = async_dal.dal
        own = await router._community_effective_tier(
            async_dal, dal, community_id=community_id, tenant="acme-corp"
        )
        other = await router._community_effective_tier(
            async_dal, dal, community_id=community_id, tenant="other-corp"
        )
        unknown = await router._community_effective_tier(
            async_dal, dal, community_id=987654, tenant="acme-corp"
        )
        assert own == "enterprise"
        assert other == unknown == "free"

    async def test_legacy_denormalized_column_is_not_a_gate(self, router_db: Any) -> None:
        """Writing `communities.license_tier` directly (nothing real does) grants nothing."""
        async_dal, community_id = router_db
        await async_dal.update_async(
            async_dal.dal.communities.id == community_id,
            license_tier="enterprise",
            is_premium=True,
        )
        dal = async_dal.dal
        assert not await router._is_professional_or_higher(
            async_dal, dal, community_id=community_id
        )
        assert not await router._is_enterprise_tier(async_dal, dal, community_id=community_id)

    async def test_resolver_failure_denies_and_logs_error(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A broken tier lookup is Free for the gate decision (never a 500, never paid) + ERROR."""
        async_dal, community_id = router_db
        await set_enterprise(async_dal, community_id)

        async def _boom(*, tenant: str, community: int | None = None) -> str:
            raise RuntimeError("license gate down")

        monkeypatch.setattr(tier_svc, "get_tier", _boom)
        # The community allocation alone is Enterprise, but the resolver fails closed as a whole.
        with caplog.at_level(logging.ERROR):
            assert not await router._is_professional_or_higher(
                async_dal, async_dal.dal, community_id=community_id
            )
            assert not await router._is_enterprise_tier(
                async_dal, async_dal.dal, community_id=community_id
            )
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors and all("RuntimeError" in r.getMessage() for r in errors)

    async def test_lookup_error_in_gate_itself_denies_and_logs(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async_dal, community_id = router_db

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("db gone")

        monkeypatch.setattr(async_dal, "select_async", _boom)
        with caplog.at_level(logging.ERROR):
            assert not await router._is_enterprise_tier(
                async_dal, async_dal.dal, community_id=community_id
            )
        assert any("ConnectionError" in r.getMessage() for r in caplog.records)

    async def test_two_tenants_sharing_a_display_name_cannot_cross_grant(
        self, router_db: Any
    ) -> None:
        """Numeric-id keying: an allocation on tenant A's community never lifts tenant B's."""
        async_dal, community_id = router_db
        dal = async_dal.dal
        twin = dal.tenants.insert(slug="acme-twin", display_name="Acme Corp", is_active=True)
        twin_community = dal.communities.insert(name="acme", tenant_id=twin, is_active=True)
        dal.commit()
        await set_enterprise(async_dal, community_id)
        assert await router._is_enterprise_tier(async_dal, dal, community_id=community_id)
        assert not await router._is_enterprise_tier(async_dal, dal, community_id=twin_community)

    async def test_slug_change_cannot_cross_grant_or_revoke(
        self, router_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Renaming a tenant slug keeps the allocation; the old slug's uplift does not follow."""
        async_dal, community_id = router_db
        dal = async_dal.dal
        await set_tier(async_dal, community_id, PRO_CODE)
        dal(dal.tenants.slug == "acme-corp").update(slug="acme-renamed")
        dal.commit()
        patch_tenant_tier(monkeypatch, {"acme-corp": "enterprise"})  # the OLD slug's license
        assert await router._is_professional_or_higher(async_dal, dal, community_id=community_id)
        assert not await router._is_enterprise_tier(async_dal, dal, community_id=community_id)

    async def test_unknown_community_fails_closed(self, router_db: Any) -> None:
        async_dal, _ = router_db
        assert not await router._is_professional_or_higher(
            async_dal, async_dal.dal, community_id=987654
        )
