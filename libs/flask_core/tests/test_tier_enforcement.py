"""
Licensing-tier enforcement tests (SECURITY: licensing enforcement).

Pins the fix for the HIGH compliance/revenue blocker where Waddles license
tiers were never enforced at runtime: ``EntitlementClient.tier_requirements``
defaulted to ``{}`` and each Feature contract's ``min_tier`` was never bridged
to it, so an Enterprise feature was gated by its PostHog flag ALONE -- a Free
tenant with the flag on got Enterprise.

What is proven here, and how it stays honest:

* ``TestCatalogMatchesContracts`` -- the static ``tier_catalog`` equals every
  one of the eight modules' real contracts (the drift guard), so enforcement
  holds even where the registry is empty (hub-api never imports the modules).
* ``TestTierEnforcement`` -- Free+flag denied / Pro vs Enterprise, run over
  EVERY contract (denominator printed in the assertion messages), not a sample.
* ``TestEffectiveTierCascade`` -- ``max(tenant, community)``, cascading down.
* ``TestFailClosed`` -- outage never grants; last-known tier is honoured within
  grace; a licensed feature never falls back to the caller's ``default``.
* ``TestBypassAndEnvUnchanged`` -- domain bypass depth intact; no env var lifts
  a tier.
* ``TestStatutoryRightsUngated`` -- DSAR/erasure/Do-Not-Sell/consent stay free.
* ``TestRealLicenseClientIntegration`` -- the real ``penguin_licensing``
  ``LicenseClient`` + real ``FeatureRegistry`` populated by every module's real
  ``register_all()``; only the HTTP socket is faked.

Fail-on-purpose proof: each group was verified to go red by temporarily
reverting ``EntitlementClient.required_tier`` to ``self.tier_requirements.get(
flag_key, "free")`` (the pre-fix behaviour) -- ``TestTierEnforcement`` and the
integration group fail with the Free tenant granted Enterprise -- and by
dropping the ``tier_result is False`` veto, which fails the
flag-outage-plus-``default=True`` test. Reverted to green.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Optional

import bot_module.features as bot_features
import community_module.features as community_features
import core_platform_module.features as core_platform_features
import customer_module.features as customer_features
import event_module.features as event_features
import marketing_module.features as marketing_features
import pytest
import social_module.features as social_features
import streaming_module.features as streaming_features
from penguin_licensing import LicenseClient

import flask_core.entitlement as entitlement_module
import flask_core.feature_flags as feature_flags_module
from flask_core.app_registry import AppRegistry
from flask_core.entitlement import (
    EntitlementClient,
    PenguinLicenseGate,
    tier_check_bypassed,
)
from flask_core.feature_contract import FeatureContract
from flask_core.feature_flags import feature_enabled, get_tier
from flask_core.feature_registry import (
    REASON_DUPLICATE_FLAG,
    FeatureRegistry,
    FeatureRegistryError,
    entitled_features,
)
from flask_core.tier_catalog import (
    FEATURE_MIN_TIERS,
    required_level,
    tier_level,
    tier_requirements_from_contracts,
)

MODULES = (
    bot_features,
    social_features,
    marketing_features,
    customer_features,
    core_platform_features,
    community_features,
    event_features,
    streaming_features,
)

ALL_CONTRACTS: tuple[FeatureContract, ...] = tuple(
    contract for module in MODULES for contract in module.build_contracts()
)
LICENSED_CONTRACTS = tuple(c for c in ALL_CONTRACTS if c.min_tier != "free")

TENANT = "acme"
ENTERPRISE_FLAG = "waddles.analytics.advanced"
PROFESSIONAL_FLAG = "waddles.community.loyalty"
FREE_FLAG = "waddles.community.polls"
UNCATALOGUED_FLAG = "waddles.webui.modular_nav"

# A host that is not a bypass domain -- every enforcement test uses it so the
# tier gate genuinely runs (a missing request_host is also "none", but being
# explicit documents intent).
PLAIN_HOST = "app.example.com"


# ---------------------------------------------------------------------------
# Fakes -- adapter seams only; the EntitlementClient under test is always real.
# ---------------------------------------------------------------------------
class FakeFlagGate:
    """PostHog stand-in: fixed result (None = unresolvable) or a raised error."""

    def __init__(self, result: Optional[bool] = True, raises: Optional[Exception] = None) -> None:
        self.result = result
        self.raises = raises
        self.calls = 0

    def is_enabled(
        self, flag_key: str, distinct_id: str, *, groups: Optional[Mapping[str, str]] = None
    ) -> Optional[bool]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


class FakeLicenseGate:
    """License-server stand-in: mutable tier or a raised error; counts calls."""

    def __init__(self, tier: str = "free", raises: Optional[Exception] = None) -> None:
        self.tier = tier
        self.raises = raises
        self.calls = 0

    def resolve_tier(self) -> str:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.tier


class FakeCommunitySource:
    """Per-community allocation stand-in: ``{(tenant, community): tier-or-None}``."""

    def __init__(
        self,
        allocations: Optional[Mapping[tuple[str, int], Optional[str]]] = None,
        raises: Optional[Exception] = None,
    ) -> None:
        self.allocations = dict(allocations or {})
        self.raises = raises
        self.calls = 0

    async def community_tier(self, tenant: str, community: int) -> Optional[str]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.allocations.get((tenant, community))


def make_client(
    *,
    tier: str = "free",
    flag: Optional[bool] = True,
    registry: Optional[FeatureRegistry] = None,
    tier_requirements: Optional[Mapping[str, str]] = None,
    community_source: Optional[FakeCommunitySource] = None,
    flag_raises: Optional[Exception] = None,
    tier_raises: Optional[Exception] = None,
) -> tuple[EntitlementClient, FakeFlagGate, FakeLicenseGate]:
    """A real client over fake gates and an ISOLATED (default: empty) registry."""
    flag_gate = FakeFlagGate(result=flag, raises=flag_raises)
    license_gate = FakeLicenseGate(tier=tier, raises=tier_raises)
    client = EntitlementClient(
        flag_gate=flag_gate,
        license_gate=license_gate,
        tier_requirements=tier_requirements or {},
        community_tier_source=community_source,
        feature_registry=registry if registry is not None else FeatureRegistry(),
    )
    return client, flag_gate, license_gate


async def allowed(client: EntitlementClient, flag: str, **kwargs: object) -> bool:
    """`evaluate` on a non-bypass host with the default `default=False`."""
    kwargs.setdefault("request_host", PLAIN_HOST)
    return await client.evaluate(flag, tenant=TENANT, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Catalog <-> contracts drift guard
# ---------------------------------------------------------------------------
class TestCatalogMatchesContracts:
    def test_denominator_is_the_full_catalog(self) -> None:
        # 54 contracts, 23 non-free -- a gate over zero examined items is a fail.
        assert len(ALL_CONTRACTS) == 54
        assert len(LICENSED_CONTRACTS) == 23
        assert len(FEATURE_MIN_TIERS) == 23

    @pytest.mark.parametrize("contract", ALL_CONTRACTS, ids=lambda c: c.id)
    def test_required_tier_equals_contract_min_tier_from_catalog_alone(
        self, contract: FeatureContract
    ) -> None:
        """Empty registry (hub-api's reality) -- the catalog alone must enforce every contract."""
        client, _, _ = make_client(registry=FeatureRegistry())
        assert client.required_tier(contract.flag) == contract.min_tier

    def test_catalog_keys_are_exactly_the_non_free_contract_flags(self) -> None:
        """Both directions: no missing non-free flag, no stale/extra catalog entry."""
        assert set(FEATURE_MIN_TIERS) == {c.flag for c in LICENSED_CONTRACTS}
        assert all(tier in {"professional", "enterprise"} for tier in FEATURE_MIN_TIERS.values())

    def test_catalog_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            FEATURE_MIN_TIERS["waddles.analytics.advanced"] = "free"  # type: ignore[index]

    def test_tier_requirements_from_contracts_covers_every_contract(self) -> None:
        derived = tier_requirements_from_contracts(ALL_CONTRACTS)
        assert derived == {c.flag: c.min_tier for c in ALL_CONTRACTS}

    def test_tier_requirements_from_contracts_keeps_the_stricter_duplicate(self) -> None:
        low = FeatureContract("x.y", 1, "bot", frozenset({"s"}), "professional", "waddles.x.y")
        high = FeatureContract("x.y", 1, "bot", frozenset({"s"}), "enterprise", "waddles.x.y")
        assert tier_requirements_from_contracts([low, high])["waddles.x.y"] == "enterprise"
        assert tier_requirements_from_contracts([high, low])["waddles.x.y"] == "enterprise"


# ---------------------------------------------------------------------------
# THE BUG: Free + flag ON must be denied a licensed feature
# ---------------------------------------------------------------------------
class TestTierEnforcement:
    async def test_free_tenant_with_flag_on_is_denied_an_enterprise_feature(self) -> None:
        client, flag_gate, _ = make_client(tier="free", flag=True)
        assert await allowed(client, ENTERPRISE_FLAG) is False
        assert flag_gate.calls == 1  # the flag said YES -- the tier alone vetoed it

    async def test_free_tenant_with_flag_on_is_denied_a_professional_feature(self) -> None:
        client, _, _ = make_client(tier="free", flag=True)
        assert await allowed(client, PROFESSIONAL_FLAG) is False

    async def test_license_client_community_name_is_the_free_rung(self) -> None:
        client, _, _ = make_client(tier="community", flag=True)
        assert await allowed(client, PROFESSIONAL_FLAG) is False
        assert await allowed(client, FREE_FLAG) is True

    async def test_enterprise_tier_is_allowed_an_enterprise_feature(self) -> None:
        client, _, _ = make_client(tier="enterprise", flag=True)
        assert await allowed(client, ENTERPRISE_FLAG) is True

    async def test_professional_tier_is_allowed_pro_but_denied_enterprise(self) -> None:
        client, _, _ = make_client(tier="professional", flag=True)
        assert await allowed(client, PROFESSIONAL_FLAG) is True
        assert await allowed(client, ENTERPRISE_FLAG) is False

    async def test_correct_tier_but_flag_off_is_still_denied(self) -> None:
        client, _, _ = make_client(tier="enterprise", flag=False)
        assert await allowed(client, ENTERPRISE_FLAG) is False

    async def test_free_features_and_uncatalogued_flags_are_unaffected(self) -> None:
        """No regression: Free + flag still unlocks Free features and any non-contract flag."""
        client, _, _ = make_client(tier="free", flag=True)
        assert await allowed(client, FREE_FLAG) is True
        assert await allowed(client, UNCATALOGUED_FLAG) is True

    @pytest.mark.parametrize("contract", ALL_CONTRACTS, ids=lambda c: c.id)
    @pytest.mark.parametrize(
        "held_tier", ["free", "professional", "enterprise"], ids=["free", "pro", "ent"]
    )
    async def test_every_contract_at_every_tier(
        self, contract: FeatureContract, held_tier: str
    ) -> None:
        """Flag ON everywhere; the decision must be exactly `held >= min_tier`, for all 54 contracts."""
        client, _, _ = make_client(tier=held_tier, flag=True)
        expected = tier_level(held_tier) >= required_level(contract.min_tier)
        assert await allowed(client, contract.flag) is expected, (
            f"{contract.id} (min_tier={contract.min_tier}) held at {held_tier}: "
            f"expected {expected}"
        )


# ---------------------------------------------------------------------------
# required_tier: stricter-of-all-sources, fail closed on garbage
# ---------------------------------------------------------------------------
class TestRequiredTier:
    def test_unlisted_flag_requires_free(self) -> None:
        client, _, _ = make_client()
        assert client.required_tier("waddles.never.heard.of.it") == "free"

    def test_registered_contract_is_enforced_even_though_it_is_not_in_the_catalog(self) -> None:
        registry = FeatureRegistry()
        registry.register(
            FeatureContract("bot.newpaid", 1, "bot", frozenset({"s:r"}), "enterprise", "waddles.bot.newpaid")
        )
        client, _, _ = make_client(registry=registry)
        assert "waddles.bot.newpaid" not in FEATURE_MIN_TIERS
        assert client.required_tier("waddles.bot.newpaid") == "enterprise"

    async def test_registry_contract_denies_a_free_tenant_end_to_end(self) -> None:
        registry = FeatureRegistry()
        registry.register(
            FeatureContract("bot.newpaid", 1, "bot", frozenset({"s:r"}), "professional", "waddles.bot.newpaid")
        )
        client, _, _ = make_client(tier="free", flag=True, registry=registry)
        assert await allowed(client, "waddles.bot.newpaid") is False

    def test_stricter_registry_contract_beats_a_laxer_catalog_entry(self) -> None:
        registry = FeatureRegistry()
        registry.register(
            FeatureContract(
                "community.loyalty", 1, "community", frozenset({"s:r"}), "enterprise",
                "waddles.community.loyalty",
            )
        )
        client, _, _ = make_client(registry=registry)
        assert FEATURE_MIN_TIERS["waddles.community.loyalty"] == "professional"
        assert client.required_tier("waddles.community.loyalty") == "enterprise"

    def test_explicit_requirement_can_raise_but_never_lower(self) -> None:
        client, _, _ = make_client(
            tier_requirements={
                "waddles.analytics.advanced": "free",  # lax -- must NOT un-gate the contract
                UNCATALOGUED_FLAG: "enterprise",  # strict -- must gate a non-contract flag
            }
        )
        assert client.required_tier("waddles.analytics.advanced") == "enterprise"
        assert client.required_tier(UNCATALOGUED_FLAG) == "enterprise"

    async def test_explicit_requirement_enforced_end_to_end(self) -> None:
        client, _, _ = make_client(
            tier="free", flag=True, tier_requirements={UNCATALOGUED_FLAG: "professional"}
        )
        assert await allowed(client, UNCATALOGUED_FLAG) is False

    def test_unrecognised_explicit_tier_raises_at_construction(self) -> None:
        with pytest.raises(ValueError, match="enterprize"):
            EntitlementClient(
                flag_gate=FakeFlagGate(),
                license_gate=FakeLicenseGate(),
                tier_requirements={"waddles.x.y": "enterprize"},
                feature_registry=FeatureRegistry(),
            )

    async def test_unrecognised_registry_tier_is_unsatisfiable_not_free(self) -> None:
        """A corrupted contract tier must deny EVERYONE, Enterprise included -- never silently free."""
        registry = FeatureRegistry()
        registry.register(
            FeatureContract("bot.weird", 1, "bot", frozenset({"s:r"}), "platinum", "waddles.bot.weird")
        )
        client, _, _ = make_client(tier="enterprise", flag=True, registry=registry)
        assert required_level(client.required_tier("waddles.bot.weird")) > required_level("enterprise")
        assert await allowed(client, "waddles.bot.weird") is False

    def test_default_registry_is_the_process_singleton(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No registry injected -> the live process registry is consulted (late registration works)."""
        process_registry = FeatureRegistry()
        monkeypatch.setattr(entitlement_module, "get_feature_registry", lambda: process_registry)
        client = EntitlementClient(flag_gate=FakeFlagGate(), license_gate=FakeLicenseGate())
        assert client.required_tier("waddles.bot.late") == "free"
        process_registry.register(
            FeatureContract("bot.late", 1, "bot", frozenset({"s:r"}), "enterprise", "waddles.bot.late")
        )
        assert client.required_tier("waddles.bot.late") == "enterprise"

    def test_registry_rejects_two_contracts_sharing_a_flag(self) -> None:
        registry = FeatureRegistry()
        registry.register(
            FeatureContract("bot.a", 1, "bot", frozenset({"s:r"}), "free", "waddles.bot.shared")
        )
        with pytest.raises(FeatureRegistryError) as excinfo:
            registry.register(
                FeatureContract("bot.b", 1, "bot", frozenset({"s:r"}), "enterprise", "waddles.bot.shared")
            )
        assert excinfo.value.reason == REASON_DUPLICATE_FLAG

    def test_registry_by_flag_lookup_and_clear(self) -> None:
        registry = FeatureRegistry()
        contract = FeatureContract("bot.a", 1, "bot", frozenset({"s:r"}), "free", "waddles.bot.a")
        registry.register(contract)
        assert registry.by_flag("waddles.bot.a") is contract
        assert registry.by_flag("waddles.bot.nope") is None
        registry.clear()
        assert registry.by_flag("waddles.bot.a") is None


# ---------------------------------------------------------------------------
# effective_tier = max(tenant, community), cascading down
# ---------------------------------------------------------------------------
class TestEffectiveTierCascade:
    async def test_community_allocation_lifts_that_community_above_a_free_tenant(self) -> None:
        source = FakeCommunitySource({(TENANT, 7): "enterprise"})
        client, _, _ = make_client(tier="free", community_source=source)
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is True
        # ...but only THAT community: a sibling and the tenant-wide check stay Free.
        assert await allowed(client, ENTERPRISE_FLAG, community=8) is False
        assert await allowed(client, ENTERPRISE_FLAG) is False

    async def test_tenant_tier_cascades_down_to_every_community(self) -> None:
        source = FakeCommunitySource({(TENANT, 7): None, (TENANT, 8): "free"})
        client, _, _ = make_client(tier="enterprise", community_source=source)
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is True
        assert await allowed(client, ENTERPRISE_FLAG, community=8) is True
        assert await allowed(client, ENTERPRISE_FLAG, community=999) is True

    async def test_community_allocation_never_lowers_the_tenant_tier(self) -> None:
        source = FakeCommunitySource({(TENANT, 7): "free"})
        client, _, _ = make_client(tier="professional", community_source=source)
        assert await allowed(client, PROFESSIONAL_FLAG, community=7) is True
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is False

    async def test_tenant_wide_check_never_consults_the_community_source(self) -> None:
        source = FakeCommunitySource({(TENANT, 7): "enterprise"})
        client, _, _ = make_client(tier="free", community_source=source)
        assert await allowed(client, ENTERPRISE_FLAG, community=None) is False
        assert source.calls == 0

    async def test_effective_tier_values(self) -> None:
        source = FakeCommunitySource({(TENANT, 1): "professional", (TENANT, 2): "enterprise"})
        client, _, _ = make_client(tier="professional", community_source=source)
        assert await client.effective_tier(tenant=TENANT) == "professional"
        assert await client.effective_tier(tenant=TENANT, community=1) == "professional"
        assert await client.effective_tier(tenant=TENANT, community=2) == "enterprise"
        assert await client.effective_tier(tenant=TENANT, community=3) == "professional"

    async def test_effective_tier_requires_a_tenant(self) -> None:
        client, _, _ = make_client()
        with pytest.raises(ValueError, match="tenant"):
            await client.effective_tier(tenant="")

    async def test_unrecognised_community_tier_gives_no_uplift(self) -> None:
        source = FakeCommunitySource({(TENANT, 7): "platinum"})
        client, _, _ = make_client(tier="free", community_source=source)
        assert await allowed(client, PROFESSIONAL_FLAG, community=7) is False

    async def test_failing_community_source_degrades_to_no_uplift_not_an_error(self) -> None:
        source = FakeCommunitySource(raises=ConnectionError("db down"))
        client, _, _ = make_client(tier="free", community_source=source)
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is False
        assert source.calls == 1

    async def test_failing_community_source_reuses_last_known_allocation_within_grace(self) -> None:
        source = FakeCommunitySource({(TENANT, 7): "enterprise"})
        client, _, _ = make_client(tier="free", community_source=source)
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is True
        source.raises = ConnectionError("db down")
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is True

    async def test_removed_allocation_is_not_resurrected_by_a_later_outage(self) -> None:
        source = FakeCommunitySource({(TENANT, 7): "enterprise"})
        client, _, _ = make_client(tier="free", community_source=source)
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is True
        source.allocations[(TENANT, 7)] = None  # tenant admin de-allocated it
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is False
        source.raises = ConnectionError("db down")
        assert await allowed(client, ENTERPRISE_FLAG, community=7) is False

    async def test_get_tier_facade_reports_the_effective_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = FakeCommunitySource({(TENANT, 2): "enterprise"})
        client, _, _ = make_client(tier="professional", community_source=source)
        monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: client)
        assert await get_tier(tenant=TENANT) == "professional"
        assert await get_tier(tenant=TENANT, community=2) == "enterprise"

    async def test_get_tier_fails_closed_to_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # unknown tier string from the license server -> free, never a guess
        client, _, _ = make_client(tier="platinum")
        monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: client)
        assert await get_tier(tenant=TENANT) == "free"

        # unresolvable (gate down, cold) -> free
        down, _, _ = make_client(tier_raises=ConnectionError("down"))
        monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: down)
        assert await get_tier(tenant=TENANT) == "free"

        # missing tenant / any unexpected error -> free, never raises
        assert await get_tier(tenant="") == "free"

        def boom() -> EntitlementClient:
            raise RuntimeError("no client")

        monkeypatch.setattr(feature_flags_module, "get_entitlement_client", boom)
        assert await get_tier(tenant=TENANT) == "free"

    async def test_get_tier_reports_enterprise_exactly_where_the_bypass_applies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _, license_gate = make_client(tier="free")
        monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: client)
        monkeypatch.setattr(
            feature_flags_module, "_current_request_host", lambda: "waddles-beta.penguintech.cloud"
        )
        assert await get_tier(tenant=TENANT, community=3) == "enterprise"
        monkeypatch.setattr(feature_flags_module, "_current_request_host", lambda: "app.waddles.app")
        assert await get_tier(tenant=TENANT) == "enterprise"  # tenant-wide: bypassed
        assert await get_tier(tenant=TENANT, community=3) == "free"  # community-scoped: NOT
        assert license_gate.calls == 1  # only the un-bypassed lookup reached the gate


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------
class TestFailClosed:
    async def test_license_down_and_cold_denies_a_licensed_feature_even_with_default_true(self) -> None:
        client, _, _ = make_client(flag=True, tier_raises=ConnectionError("license down"))
        assert await allowed(client, ENTERPRISE_FLAG, default=True) is False
        assert await allowed(client, PROFESSIONAL_FLAG, default=True) is False

    async def test_license_down_free_feature_still_degrades_to_default(self) -> None:
        """Preserved behaviour: only LICENSED features lose the `default` fallback."""
        client, _, _ = make_client(flag=True, tier_raises=ConnectionError("license down"))
        assert await allowed(client, FREE_FLAG, default=True) is True
        assert await allowed(client, FREE_FLAG, default=False) is False

    async def test_flag_outage_with_known_insufficient_tier_denies_despite_default_true(self) -> None:
        """THE SECOND HOLE: PostHog down + `default=True` used to grant a feature the tier forbids."""
        client, _, _ = make_client(tier="free", flag=None)
        assert await allowed(client, ENTERPRISE_FLAG, default=True) is False

    async def test_flag_outage_with_sufficient_tier_still_honours_default(self) -> None:
        client, _, _ = make_client(tier="enterprise", flag=None)
        assert await allowed(client, ENTERPRISE_FLAG, default=True) is True
        assert await allowed(client, ENTERPRISE_FLAG, default=False) is False

    async def test_both_down_cold_denies_a_licensed_feature(self) -> None:
        client, _, _ = make_client(
            flag_raises=ConnectionError("posthog down"), tier_raises=ConnectionError("license down")
        )
        assert await allowed(client, ENTERPRISE_FLAG, default=True) is False

    async def test_last_known_enterprise_tier_survives_a_license_outage(self) -> None:
        client, _, license_gate = make_client(tier="enterprise", flag=True)
        assert await allowed(client, ENTERPRISE_FLAG) is True
        license_gate.raises = ConnectionError("license down")
        assert await allowed(client, ENTERPRISE_FLAG) is True  # last-known tier, flag still live

    async def test_last_known_free_tier_never_upgrades_during_an_outage(self) -> None:
        client, _, license_gate = make_client(tier="free", flag=True)
        assert await allowed(client, ENTERPRISE_FLAG) is False
        license_gate.raises = ConnectionError("license down")
        assert await allowed(client, ENTERPRISE_FLAG, default=True) is False

    async def test_both_down_with_warm_state_serves_the_cached_decision(self) -> None:
        client, flag_gate, license_gate = make_client(tier="enterprise", flag=True)
        assert await allowed(client, ENTERPRISE_FLAG) is True
        flag_gate.raises = ConnectionError("posthog down")
        license_gate.raises = ConnectionError("license down")
        assert await allowed(client, ENTERPRISE_FLAG, default=False) is True

    async def test_last_known_tier_expires_after_the_grace_window(self) -> None:
        client, _, license_gate = make_client(tier="enterprise", flag=True)
        assert await allowed(client, ENTERPRISE_FLAG) is True
        license_gate.raises = ConnectionError("license down")
        assert await allowed(client, ENTERPRISE_FLAG) is True

        # Age the remembered tier past the grace window: now it must deny.
        for entry in client._tier_cache.values():
            entry.observed_at -= client.tier_grace_seconds + 1.0
        assert await allowed(client, ENTERPRISE_FLAG, default=True) is False

    async def test_a_revoked_tier_takes_effect_immediately_when_the_license_server_answers(self) -> None:
        """Downgrade is never masked by a stale cached grant while the server is reachable."""
        client, _, license_gate = make_client(tier="enterprise", flag=True)
        assert await allowed(client, ENTERPRISE_FLAG) is True
        license_gate.tier = "free"
        assert await allowed(client, ENTERPRISE_FLAG) is False

    async def test_unrecognised_held_tier_grants_nothing(self) -> None:
        client, _, _ = make_client(tier="platinum", flag=True)
        assert await allowed(client, ENTERPRISE_FLAG) is False
        assert await allowed(client, PROFESSIONAL_FLAG) is False

    async def test_internal_error_on_a_licensed_flag_never_grants_via_default(self) -> None:
        """An unexpected exception inside evaluate() returns False for licensed flags, `default` for free."""

        class ExplodingRegistry(FeatureRegistry):
            def by_flag(self, flag: str) -> Optional[FeatureContract]:
                raise RuntimeError("registry corrupted")

        client, _, _ = make_client(tier="enterprise", flag=True, registry=ExplodingRegistry())
        # The requirement itself is unresolvable -> treated as licensed -> denied, not defaulted.
        assert await allowed(client, ENTERPRISE_FLAG, default=True) is False

    async def test_missing_tenant_on_a_licensed_flag_denies_even_with_default_true(self) -> None:
        client, _, _ = make_client(tier="enterprise", flag=True)
        assert await client.evaluate(ENTERPRISE_FLAG, tenant="", default=True) is False
        # free flag keeps the pre-existing contract: ValueError swallowed -> default
        assert await client.evaluate(FREE_FLAG, tenant="", default=True) is True

    async def test_a_tier_denial_does_not_poison_the_decision_cache_for_a_later_upgrade(self) -> None:
        client, _, license_gate = make_client(tier="free", flag=True)
        assert await allowed(client, PROFESSIONAL_FLAG) is False
        license_gate.tier = "professional"
        assert await allowed(client, PROFESSIONAL_FLAG) is True


# ---------------------------------------------------------------------------
# Bypass domains and env vars
# ---------------------------------------------------------------------------
class TestBypassAndEnvUnchanged:
    @pytest.mark.parametrize(
        "host,community,expected",
        [
            ("waddles-beta.penguintech.cloud", None, True),
            ("waddles-beta.penguintech.cloud", 4, True),
            ("penguincloud.io", 4, True),
            ("app.waddles.app", None, True),
            ("app.waddles.app", 4, False),
            ("waddles.penguintech.cloud.attacker.com", None, False),
            ("notwaddles.app", None, False),
            ("app.example.com", None, False),
            (None, None, False),
        ],
    )
    def test_tier_check_bypassed(self, host: Optional[str], community: Optional[int], expected: bool) -> None:
        assert tier_check_bypassed(host, community) is expected

    async def test_preprod_bypass_domain_skips_the_tier_but_not_the_flag(self) -> None:
        host = "waddles-beta.penguintech.cloud"
        client, _, license_gate = make_client(tier="free", flag=True)
        assert await client.evaluate(ENTERPRISE_FLAG, tenant=TENANT, request_host=host) is True
        assert license_gate.calls == 0

        flag_off, _, _ = make_client(tier="free", flag=False)
        assert await flag_off.evaluate(ENTERPRISE_FLAG, tenant=TENANT, request_host=host) is False

    async def test_prod_domain_bypasses_tenant_wide_but_not_community_scoped_tier(self) -> None:
        client, _, license_gate = make_client(tier="free", flag=True)
        host = "app.waddles.app"
        assert await client.evaluate(ENTERPRISE_FLAG, tenant=TENANT, request_host=host) is True
        assert (
            await client.evaluate(ENTERPRISE_FLAG, tenant=TENANT, community=9, request_host=host)
            is False
        )
        assert license_gate.calls == 1

    async def test_lookalike_host_is_not_a_bypass(self) -> None:
        client, _, _ = make_client(tier="free", flag=True)
        denied = await client.evaluate(
            ENTERPRISE_FLAG, tenant=TENANT, request_host="waddles.penguintech.cloud.attacker.com"
        )
        assert denied is False

    @pytest.mark.parametrize(
        "name,value",
        [
            ("FLAG_WADDLES_ANALYTICS_ADVANCED", "true"),
            ("FLAG_WADDLES_COMMUNITY_LOYALTY", "1"),
            ("LICENSE_TIER", "enterprise"),
            ("WADDLES_TIER", "enterprise"),
            ("WADDLES_DEV", "1"),
            ("DEV_MODE", "true"),
            ("ENTITLEMENT_BYPASS", "1"),
            ("LICENSE_BYPASS", "true"),
            ("ENTITLEMENT_TIER_GRACE_SECONDS", "0"),
        ],
    )
    async def test_no_env_var_lifts_a_license_gated_feature(
        self, monkeypatch: pytest.MonkeyPatch, name: str, value: str
    ) -> None:
        """License flags are NOT env-bypassable -- only plain FEATURE flags have an ENV baseline."""
        monkeypatch.setenv(name, value)
        client = EntitlementClient(
            flag_gate=FakeFlagGate(result=True),
            license_gate=FakeLicenseGate(tier="free"),
            feature_registry=FeatureRegistry(),
        )
        assert await allowed(client, ENTERPRISE_FLAG) is False
        assert await allowed(client, PROFESSIONAL_FLAG) is False


# ---------------------------------------------------------------------------
# Statutory rights are never tier-gated
# ---------------------------------------------------------------------------
STATUTORY_KEYWORDS = re.compile(
    r"dsar|erasure|do[_-]?not[_-]?(sell|share)|consent|data[_-]?subject|right[_-]?to", re.IGNORECASE
)
STATUTORY_FLAGS = (
    "waddles.privacy.dsar",
    "waddles.privacy.erasure",
    "waddles.privacy.do_not_sell",
    "waddles.privacy.consent_withdrawal",
)
REPO_ROOT = Path(__file__).resolve().parents[3]
STATUTORY_SOURCES = (
    "hub_api/blueprints/v1/data_privacy.py",
    "hub_api/services/data_privacy_service.py",
)


class TestStatutoryRightsUngated:
    def test_no_statutory_flag_is_in_the_catalog_or_any_contract_above_free(self) -> None:
        examined = list(FEATURE_MIN_TIERS) + [c.flag for c in ALL_CONTRACTS] + [c.id for c in ALL_CONTRACTS]
        assert len(examined) > 100  # denominator: a scan of nothing proves nothing
        offenders = [
            flag
            for flag in FEATURE_MIN_TIERS
            if STATUTORY_KEYWORDS.search(flag)
        ] + [
            c.id
            for c in ALL_CONTRACTS
            if c.min_tier != "free" and STATUTORY_KEYWORDS.search(c.id)
        ]
        assert offenders == []

    @pytest.mark.parametrize("flag", STATUTORY_FLAGS)
    async def test_statutory_flags_require_free_and_unlock_for_a_free_tenant(self, flag: str) -> None:
        client, _, _ = make_client(tier="free", flag=True)
        assert client.required_tier(flag) == "free"
        assert await allowed(client, flag) is True

    @pytest.mark.parametrize("source", STATUTORY_SOURCES)
    def test_statutory_endpoints_never_call_the_entitlement_gate(self, source: str) -> None:
        path = REPO_ROOT / source
        text = path.read_text(encoding="utf-8")
        assert len(text) > 500, f"{source} unexpectedly empty -- scanner pointed at the wrong file"
        for token in ("feature_enabled", "get_entitlement_client", "EntitlementClient", "get_tier"):
            assert token not in text, f"{source} gates a statutory right via {token}"


# ---------------------------------------------------------------------------
# Integration: real penguin_licensing LicenseClient + real registry/contracts
# ---------------------------------------------------------------------------
class _FakeHttpResponse:
    """The only fake in the integration group: the HTTP socket to license.penguintech.io."""

    def __init__(self, status_code: int, payload: Optional[dict[str, object]] = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "fake"

    def json(self) -> dict[str, object]:
        return self._payload


def _license_payload(tier: str) -> dict[str, object]:
    return {
        "customer": "acme",
        "product": "waddles",
        "license_version": "2.0",
        "license_key": "PENG-TEST-0000",
        "expires_at": "2099-01-01T00:00:00Z",
        "issued_at": "2026-01-01T00:00:00Z",
        "tier": tier,
        "features": [],
    }


def _real_license_client(*, key: str) -> LicenseClient:
    return LicenseClient(license_key=key, product="waddles", base_url="https://license.penguintech.io")


def _wire_real_stack(license_client: object) -> tuple[EntitlementClient, FeatureRegistry]:
    """Real EntitlementClient + PenguinLicenseGate + registry filled by every module's register_all()."""
    registry, apps = FeatureRegistry(), AppRegistry()
    for module in MODULES:
        module.register_all(feature_registry=registry, app_registry=apps)
    client = EntitlementClient(
        flag_gate=FakeFlagGate(result=True),
        license_gate=PenguinLicenseGate(license_client),
        feature_registry=registry,
    )
    return client, registry


class TestRealLicenseClientIntegration:
    async def test_no_license_key_is_community_and_denies_licensed_features(self) -> None:
        client, _ = _wire_real_stack(_real_license_client(key=""))
        assert await allowed(client, ENTERPRISE_FLAG) is False
        assert await allowed(client, PROFESSIONAL_FLAG) is False
        assert await allowed(client, FREE_FLAG) is True

    @pytest.mark.parametrize(
        "server_tier,pro_ok,ent_ok",
        [("community", False, False), ("professional", True, False), ("enterprise", True, True)],
    )
    async def test_server_reported_tier_drives_the_decision(
        self, server_tier: str, pro_ok: bool, ent_ok: bool
    ) -> None:
        lic = _real_license_client(key="PENG-TEST-0000")
        lic.session.post = lambda *a, **k: _FakeHttpResponse(200, _license_payload(server_tier))  # type: ignore[method-assign,assignment]
        client, _ = _wire_real_stack(lic)
        assert await allowed(client, PROFESSIONAL_FLAG) is pro_ok
        assert await allowed(client, ENTERPRISE_FLAG) is ent_ok

    async def test_license_server_rejection_denies(self) -> None:
        lic = _real_license_client(key="PENG-REVOKED")
        lic.session.post = lambda *a, **k: _FakeHttpResponse(403)  # type: ignore[method-assign,assignment]
        client, _ = _wire_real_stack(lic)
        assert await allowed(client, ENTERPRISE_FLAG) is False

    async def test_license_server_outage_never_grants(self) -> None:
        def unreachable(*args: object, **kwargs: object) -> _FakeHttpResponse:
            raise ConnectionError("license.penguintech.io unreachable")

        lic = _real_license_client(key="PENG-TEST-0000")
        lic.session.post = unreachable  # type: ignore[method-assign,assignment]
        client, _ = _wire_real_stack(lic)
        assert await allowed(client, ENTERPRISE_FLAG, default=True) is False

    async def test_feature_enabled_facade_enforces_the_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lic = _real_license_client(key="PENG-TEST-0000")
        lic.session.post = lambda *a, **k: _FakeHttpResponse(200, _license_payload("professional"))  # type: ignore[method-assign,assignment]
        client, _ = _wire_real_stack(lic)
        monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: client)
        assert await feature_enabled(PROFESSIONAL_FLAG, tenant=TENANT) is True
        assert await feature_enabled(ENTERPRISE_FLAG, tenant=TENANT) is False
        assert await get_tier(tenant=TENANT) == "professional"

    async def test_mcp_tool_listing_no_longer_leaks_licensed_features_to_a_free_tenant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`entitled_features` (the per-tenant MCP tool source) over the REAL 54-contract registry."""
        free_lic = _real_license_client(key="")
        client, registry = _wire_real_stack(free_lic)
        monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: client)
        assert len(registry.all_contracts()) == 54

        visible = await entitled_features(tenant=TENANT, contracts=registry.all_contracts())
        assert {c.id for c in visible} == {c.id for c in ALL_CONTRACTS if c.min_tier == "free"}
        assert len(visible) == 54 - 23

        ent_lic = _real_license_client(key="PENG-TEST-0000")
        ent_lic.session.post = lambda *a, **k: _FakeHttpResponse(200, _license_payload("enterprise"))  # type: ignore[method-assign,assignment]
        ent_client, _ = _wire_real_stack(ent_lic)
        monkeypatch.setattr(feature_flags_module, "get_entitlement_client", lambda: ent_client)
        assert len(await entitled_features(tenant=TENANT, contracts=registry.all_contracts())) == 54

    async def test_default_singleton_client_enforces_without_any_registry_or_wiring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """hub-api's reality: `get_entitlement_client()` default, empty registry, nothing registered."""
        monkeypatch.setattr(entitlement_module, "_default_client", None)
        monkeypatch.setattr(entitlement_module, "get_feature_registry", lambda: FeatureRegistry())
        monkeypatch.setattr(
            entitlement_module.PostHogFlagGate, "from_env", classmethod(lambda cls: FakeFlagGate(True))
        )
        monkeypatch.setattr(
            entitlement_module.PenguinLicenseGate,
            "from_env",
            classmethod(lambda cls: FakeLicenseGate("free")),
        )
        client = entitlement_module.get_entitlement_client()
        assert await allowed(client, ENTERPRISE_FLAG) is False
        assert await allowed(client, FREE_FLAG) is True
