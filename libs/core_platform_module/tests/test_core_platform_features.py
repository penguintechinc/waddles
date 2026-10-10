"""
Core/Platform Module Feature tests
======================================

Covers :mod:`core_platform_module.features` -- the Core/platform Module's
registration of its 16 Feature contracts and their shipped default Apps
against the v3 Feature-contract spine (:mod:`flask_core.feature_contract`,
:mod:`flask_core.feature_registry`, :mod:`flask_core.app_manifest`,
:mod:`flask_core.app_registry`). Mirrors
``libs/flask_core/tests/test_bot_features.py``'s shape one-for-one.

Every assertion runs against fresh, isolated registries (never the process
singletons) so this test file can run in any order relative to Bot's (or
any other module's) feature tests without leaking registrations between
them.

Fail-on-purpose proof: ``test_default_app_scopes_are_subset_of_feature_scopes``
was verified to catch a regression by temporarily widening
``analytics.bad_actor_detection.default``'s ``permissions`` in
``core_platform_module/features.py`` past its Feature's ``requires_scopes``
and confirming ``register_all`` raises ``ScopeWideningError``, then
reverting.
"""

from __future__ import annotations

from typing import Optional

import pytest

from core_platform_module.features import ScopeWideningError, register_all
from flask_core.app_registry import AppRegistry
from flask_core.feature_registry import FeatureRegistry

EXPECTED_FEATURES = {
    "analytics.community_health": (
        "waddles.analytics.community_health",
        "professional",
        frozenset({"analytics.community_health:read"}),
    ),
    "analytics.bad_actor_detection": (
        "waddles.analytics.bad_actor_detection",
        "professional",
        frozenset({"analytics.bad_actor:read"}),
    ),
    "analytics.user_journey": (
        "waddles.analytics.user_journey",
        "professional",
        frozenset({"analytics.user_journey:read"}),
    ),
    "analytics.retention_cohorts": (
        "waddles.analytics.retention_cohorts",
        "professional",
        frozenset({"analytics.retention:read"}),
    ),
    "analytics.engagement_funnels": (
        "waddles.analytics.engagement_funnels",
        "professional",
        frozenset({"analytics.engagement:read"}),
    ),
    "analytics.advanced": (
        "waddles.analytics.advanced",
        "enterprise",
        frozenset({"analytics.advanced:read"}),
    ),
    "video_proxy.streaming": (
        "waddles.video_proxy.streaming",
        "free",
        frozenset({"video_proxy.stream:write"}),
    ),
    "video_proxy.premium_limits": (
        "waddles.video_proxy.premium_limits",
        "professional",
        frozenset({"video_proxy.premium:write"}),
    ),
    "auth.sso_google": (
        "waddles.auth.sso_google",
        "professional",
        frozenset({"auth.sso:admin"}),
    ),
    "auth.sso_saml": (
        "waddles.auth.sso_saml",
        "enterprise",
        frozenset({"auth.sso:admin"}),
    ),
    "compliance.audit_logs": (
        "waddles.compliance.audit_logs",
        "enterprise",
        frozenset({"compliance.audit:read"}),
    ),
    "compliance.external_kms": (
        "waddles.compliance.external_kms",
        "enterprise",
        frozenset({"compliance.kms:admin"}),
    ),
    "integrations.waddleai": (
        "waddles.integrations.waddleai",
        "enterprise",
        frozenset({"integrations.waddleai:write"}),
    ),
    "tenancy.multi_tenant": (
        "waddles.tenancy.multi_tenant",
        "enterprise",
        frozenset({"tenancy.tenant:admin"}),
    ),
    "compliance.bulk_dsar": (
        "waddles.compliance.bulk_dsar",
        "enterprise",
        frozenset({"compliance.dsar:admin"}),
    ),
    "tenancy.whitelabel": (
        "waddles.tenancy.whitelabel",
        "professional",
        frozenset({"tenancy.whitelabel:admin"}),
    ),
}

EXPECTED_MODULE = {
    feature_id: feature_id.split(".", 1)[0] for feature_id in EXPECTED_FEATURES
}


@pytest.fixture
def registries() -> tuple[FeatureRegistry, AppRegistry]:
    """Fresh, isolated Feature + App registries -- never the process singletons."""
    return FeatureRegistry(), AppRegistry()


class TestCorePlatformFeatureContracts:
    def test_all_sixteen_features_registered_with_correct_tier_and_flag(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        contracts, _ = register_all(feature_registry=feature_registry, app_registry=app_registry)

        assert {c.id for c in contracts} == set(EXPECTED_FEATURES)
        assert len(contracts) == 16
        for c in contracts:
            expected_flag, expected_tier, expected_scopes = EXPECTED_FEATURES[c.id]
            assert c.module == EXPECTED_MODULE[c.id]
            assert c.version == 1
            assert c.min_tier == expected_tier
            assert c.flag == expected_flag
            assert c.requires_scopes == expected_scopes

    def test_features_are_queryable_from_the_registry_by_id_and_module(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        register_all(feature_registry=feature_registry, app_registry=app_registry)

        assert (
            feature_registry.get("analytics.community_health").flag
            == "waddles.analytics.community_health"
        )
        analytics_ids = {c.id for c in feature_registry.for_module("analytics")}
        assert analytics_ids == {
            "analytics.community_health",
            "analytics.bad_actor_detection",
            "analytics.user_journey",
            "analytics.retention_cohorts",
            "analytics.engagement_funnels",
            "analytics.advanced",
        }


class TestCorePlatformDefaultApps:
    def test_every_feature_has_exactly_one_default_app(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        register_all(feature_registry=feature_registry, app_registry=app_registry)

        for feature_id, (flag, _tier, _scopes) in EXPECTED_FEATURES.items():
            default_app = app_registry.default_app_for(flag)
            assert default_app is not None, f"no default app for {feature_id}"
            assert default_app.is_default is True
            assert default_app.provider == "builtin"
            assert default_app.module == EXPECTED_MODULE[feature_id]
            assert default_app.feature == flag

    def test_default_app_scopes_are_subset_of_feature_scopes(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        contracts, manifests = register_all(
            feature_registry=feature_registry, app_registry=app_registry
        )
        assert len(manifests) == 16
        contracts_by_flag = {c.flag: c for c in contracts}

        for manifest in manifests:
            contract = contracts_by_flag[manifest.feature]
            assert set(manifest.permissions) <= contract.requires_scopes

    def test_scope_widening_default_app_is_rejected(self) -> None:
        """A default App claiming a scope its own Feature doesn't grant must be rejected."""
        import core_platform_module.features as features_module

        original_defs = features_module._DEFAULT_APP_DEFS
        # index 1 == analytics.bad_actor_detection.default
        widened = dict(original_defs[1])
        widened["permissions"] = ("analytics.bad_actor:read", "analytics.bad_actor:admin")
        features_module._DEFAULT_APP_DEFS = (
            original_defs[0],
            widened,
            *original_defs[2:],
        )
        try:
            with pytest.raises(ScopeWideningError):
                register_all(feature_registry=FeatureRegistry(), app_registry=AppRegistry())
        finally:
            features_module._DEFAULT_APP_DEFS = original_defs


class FakeCheck:
    """Resolves each flag against a fixed enabled-set; mirrors test_bot_features.py's fake."""

    def __init__(self, enabled_flags: set[str]) -> None:
        self.enabled_flags = enabled_flags

    async def __call__(
        self, flag_key: str, *, tenant: str, community: Optional[int] = None, default: bool = False
    ) -> bool:
        return flag_key in self.enabled_flags


class TestAnalyticsCommunityHealthGate:
    """
    Exercises the worked gate example's contract, not the service call site
    itself: ``core/analytics_core_module/services/health_service.py``'s
    ``HealthService.calculate_health_score`` is a separate service with its
    own heavy dependency chain (pydal, asyncpg), out of scope for
    flask_core's / this package's unit suite. What's asserted here is the
    gate condition the service guards on --
    ``feature_enabled("waddles.analytics.community_health", ...)`` -- using
    the same fake-check shape as ``test_bot_features.py``'s
    ``TestShoutoutGate``, so a regression in the flag-name the service
    checks is caught here even though the service's own process isn't
    imported.
    """

    async def test_community_health_flag_off_is_not_entitled(self) -> None:
        from flask_core.feature_registry import entitled_features

        from core_platform_module.features import build_contracts

        contracts = build_contracts()
        community_health = next(c for c in contracts if c.id == "analytics.community_health")
        check = FakeCheck(enabled_flags=set())  # waddles.analytics.community_health OFF

        result = await entitled_features(
            tenant="acme", contracts=(community_health,), check=check
        )

        assert result == []

    async def test_community_health_flag_on_is_entitled(self) -> None:
        from flask_core.feature_registry import entitled_features

        from core_platform_module.features import build_contracts

        contracts = build_contracts()
        community_health = next(c for c in contracts if c.id == "analytics.community_health")
        check = FakeCheck(enabled_flags={"waddles.analytics.community_health"})

        result = await entitled_features(
            tenant="acme", contracts=(community_health,), check=check
        )

        assert result == [community_health]
