"""
Cross-module Feature-registration coherence gate
====================================================

v3.0.0 MVP gate, grown under SCCEBM P4
(docs/plans/2026-08-31-v3-sccebm-program-plan.md SS9): registers all
EIGHT Modules' Feature contracts (Bot, Social, Marketing, Customer,
Core/Platform, Community, Event, Streaming -- ``bot_module``,
``social_module``, ``marketing_module``, ``customer_module``,
``core_platform_module``, ``community_module``, ``event_module``,
``streaming_module``, each mirroring :mod:`bot_module.features`'s shape
one-for-one) against a single, fresh, shared
:class:`~flask_core.feature_registry.FeatureRegistry` /
:class:`~flask_core.app_registry.AppRegistry` pair -- never the process
singletons -- and asserts the whole catalog (54 Feature contracts: bot 4 +
social 9 + marketing 3 + customer 5 + core_platform 15 + community 11 +
event 2 + streaming 5) is internally coherent: no id/app_id collisions
across Modules, every contract's flag and tier/module are well-formed, and
every default App's permissions stay inside its own Feature's granted
scopes. Per-module suites (``test_bot_features.py``, ``libs/social_module/
tests/test_social_features.py``, ``libs/community_module/tests/
test_community_features.py``, etc.) already cover each Module in
isolation; this file is the one place that proves they compose without
collision when loaded together, the way a real process startup would.

Fail-on-purpose proof: ``test_total_registered_feature_count_is_54`` was
verified to catch a regression by temporarily asserting ``== 99`` instead
of ``== 54`` and confirming the test fails with the true count reported,
then reverting.
"""

from __future__ import annotations

import bot_module.features as bot_features
import community_module.features as community_features
import core_platform_module.features as core_platform_features
import customer_module.features as customer_features
import event_module.features as event_features
import marketing_module.features as marketing_features
import pytest
import social_module.features as social_features
import streaming_module.features as streaming_features
from flask_core.app_manifest import KNOWN_MODULES
from flask_core.app_registry import AppRegistry
from flask_core.feature_registry import FeatureRegistry

KNOWN_TIERS = frozenset({"free", "professional", "enterprise"})

# (module label, features submodule) -- registration order mirrors the
# order each Module's PR landed in (see docs/plans/2026-08-26-v3-scbm-apps-
# design.md migration phase P1 onward, then docs/plans/2026-08-31-v3-
# sccembs-program-plan.md SS9 P4): bot, social, marketing, customer,
# core_platform, then the three P4 additions (community, event, streaming).
MODULES = (
    ("bot", bot_features),
    ("social", social_features),
    ("marketing", marketing_features),
    ("customer", customer_features),
    ("core_platform", core_platform_features),
    ("community", community_features),
    ("event", event_features),
    ("streaming", streaming_features),
)

EXPECTED_COUNTS = {
    "bot": 4,
    "social": 9,
    "marketing": 3,
    "customer": 5,
    "core_platform": 15,
    "community": 11,
    "event": 2,
    "streaming": 5,
}
TOTAL_EXPECTED = 54
assert sum(EXPECTED_COUNTS.values()) == TOTAL_EXPECTED  # keep the two constants honest


@pytest.fixture
def registries() -> tuple[FeatureRegistry, AppRegistry]:
    """Fresh, isolated Feature + App registries -- never the process singletons."""
    return FeatureRegistry(), AppRegistry()


def _register_everything(
    feature_registry: FeatureRegistry, app_registry: AppRegistry
) -> dict[str, int]:
    """Register all five Modules against the given shared registries.

    Returns each Module's registered-contract count, keyed by label. A
    duplicate feature id or app_id across Modules would raise
    ``FeatureRegistryError``/``RegistryError`` here (the registries reject
    duplicates at ``register()`` time) -- this function succeeding at all
    is itself part of the collision proof; ``test_no_duplicate_*`` below
    additionally makes that check explicit and independent of registration
    order.
    """
    counts: dict[str, int] = {}
    for label, features_module in MODULES:
        contracts, _manifests = features_module.register_all(
            feature_registry=feature_registry, app_registry=app_registry
        )
        counts[label] = len(contracts)
    return counts


class TestAllModulesRegisterCoherently:
    def test_total_registered_feature_count_is_54(
        self, registries: tuple[FeatureRegistry, AppRegistry], capsys: pytest.CaptureFixture[str]
    ) -> None:
        feature_registry, app_registry = registries
        counts = _register_everything(feature_registry, app_registry)

        print(f"per-module Feature counts: {counts}")
        for label, expected in EXPECTED_COUNTS.items():
            assert counts[label] == expected, f"{label}: expected {expected}, got {counts[label]}"

        total = sum(counts.values())
        assert total == len(feature_registry.all_contracts())
        assert total == TOTAL_EXPECTED

    def test_no_duplicate_feature_id_across_modules(self) -> None:
        """Collision check, independent of registration order/side effects.

        Built via each Module's own ``build_contracts()`` (validates, does
        not register), so this holds even if a future refactor changes
        registration order or registry duplicate-detection behavior.
        """
        all_ids = [
            contract.id
            for _label, features_module in MODULES
            for contract in features_module.build_contracts()
        ]
        assert len(all_ids) == TOTAL_EXPECTED
        duplicates = {fid for fid in all_ids if all_ids.count(fid) > 1}
        assert not duplicates, f"duplicate feature id(s) across modules: {sorted(duplicates)}"
        assert len(all_ids) == len(set(all_ids))

    def test_no_duplicate_app_id_across_modules(self) -> None:
        all_app_ids = [
            manifest.app_id
            for _label, features_module in MODULES
            for manifest in features_module.build_default_apps()
        ]
        assert len(all_app_ids) == TOTAL_EXPECTED
        duplicates = {aid for aid in all_app_ids if all_app_ids.count(aid) > 1}
        assert not duplicates, f"duplicate app_id(s) across modules: {sorted(duplicates)}"
        assert len(all_app_ids) == len(set(all_app_ids))

    def test_every_contract_flag_is_waddles_prefixed_id(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        _register_everything(feature_registry, app_registry)

        for contract in feature_registry.all_contracts():
            assert contract.flag == f"waddles.{contract.id}"

    def test_every_default_app_permissions_is_subset_of_its_features_scopes(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        _register_everything(feature_registry, app_registry)

        for contract in feature_registry.all_contracts():
            default_app = app_registry.default_app_for(contract.flag)
            assert default_app is not None, f"no default app registered for {contract.id}"
            assert set(default_app.permissions) <= contract.requires_scopes

    def test_every_contract_min_tier_and_module_are_known(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        _register_everything(feature_registry, app_registry)

        for contract in feature_registry.all_contracts():
            assert contract.min_tier in KNOWN_TIERS, f"{contract.id}: bad min_tier {contract.min_tier!r}"
            assert contract.module in KNOWN_MODULES, f"{contract.id}: bad module {contract.module!r}"
