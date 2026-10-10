"""
Module loader tests
======================

Covers :mod:`flask_core.module_loader` -- the app-side read side of
"Modules toggle globally": Core/Platform always registers, each of the
four toggleable Modules (Bot, Social, Marketing, Customer) registers only
when enabled, and env-var parsing matches the Helm template's
``MODULE_LOAD_BOT/SOCIAL/MARKETING/CUSTOMER`` shape exactly (present +
truthy = enabled, absent = disabled).

Every assertion runs against fresh, isolated registries (never the process
singletons), same convention as ``test_all_modules_register.py`` and each
Module's own feature test file.

Fail-first proof: ``test_disabling_one_module_removes_only_its_contracts``
was verified to catch a regression by temporarily deleting the
``if module not in resolved: continue`` skip in
``module_loader.load_enabled_modules`` (so every toggleable Module
registered unconditionally, reproducing the pre-fix bug) and confirming
the test failed with social's 9 contracts still present, then reverting.
"""

from __future__ import annotations

import pytest

from flask_core.app_registry import AppRegistry
from flask_core.feature_registry import FeatureRegistry
from flask_core.module_loader import (
    TOGGLEABLE_MODULES,
    enabled_modules_from_env,
    load_enabled_modules,
)

EXPECTED_COUNTS = {
    "bot": 4,
    "social": 9,
    "marketing": 3,
    "customer": 5,
    "core_platform": 15,
}
TOTAL_ALL_ENABLED = 36
assert sum(EXPECTED_COUNTS.values()) == TOTAL_ALL_ENABLED  # keep constants honest


@pytest.fixture
def registries() -> tuple[FeatureRegistry, AppRegistry]:
    """Fresh, isolated Feature + App registries -- never the process singletons."""
    return FeatureRegistry(), AppRegistry()


class TestAllEnabled:
    def test_explicit_all_four_registers_every_module_including_core_platform(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        results = load_enabled_modules(
            enabled=set(TOGGLEABLE_MODULES),
            feature_registry=feature_registry,
            app_registry=app_registry,
        )

        assert set(results) == {"core_platform", "bot", "social", "marketing", "customer"}
        for label, expected in EXPECTED_COUNTS.items():
            contracts, _manifests = results[label]
            assert len(contracts) == expected, f"{label}: expected {expected}, got {len(contracts)}"

        assert len(feature_registry.all_contracts()) == TOTAL_ALL_ENABLED

    def test_nothing_specified_defaults_to_all_four_enabled(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        """No `enabled=` and no MODULE_LOAD_* in `env` -- fails open to everything on."""
        feature_registry, app_registry = registries
        results = load_enabled_modules(
            env={},
            feature_registry=feature_registry,
            app_registry=app_registry,
        )

        assert set(results) == {"core_platform", "bot", "social", "marketing", "customer"}
        assert len(feature_registry.all_contracts()) == TOTAL_ALL_ENABLED

    def test_env_with_all_four_module_load_vars_present_registers_all(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        env = {
            "MODULE_LOAD_BOT": "true",
            "MODULE_LOAD_SOCIAL": "true",
            "MODULE_LOAD_MARKETING": "true",
            "MODULE_LOAD_CUSTOMER": "true",
        }
        results = load_enabled_modules(
            env=env, feature_registry=feature_registry, app_registry=app_registry
        )

        assert set(results) == {"core_platform", "bot", "social", "marketing", "customer"}
        assert len(feature_registry.all_contracts()) == TOTAL_ALL_ENABLED


class TestOneModuleDisabled:
    def test_disabling_one_module_removes_only_its_contracts(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        results = load_enabled_modules(
            enabled={"bot", "marketing", "customer"},  # social omitted
            feature_registry=feature_registry,
            app_registry=app_registry,
        )

        assert set(results) == {"core_platform", "bot", "marketing", "customer"}
        assert "social" not in results

        assert feature_registry.for_module("social") == ()
        assert len(feature_registry.all_contracts()) == TOTAL_ALL_ENABLED - EXPECTED_COUNTS["social"]

        # The other three toggleable Modules plus core_platform are untouched.
        assert len(feature_registry.for_module("bot")) == EXPECTED_COUNTS["bot"]
        assert len(feature_registry.for_module("marketing")) == EXPECTED_COUNTS["marketing"]
        assert len(feature_registry.for_module("customer")) == EXPECTED_COUNTS["customer"]

    def test_disabled_module_has_no_default_apps_registered_either(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        load_enabled_modules(
            enabled={"social", "marketing", "customer"},  # bot omitted
            feature_registry=feature_registry,
            app_registry=app_registry,
        )

        assert app_registry.apps_for_feature("waddles.bot.shoutout") == ()
        assert app_registry.default_app_for("waddles.bot.shoutout") is None

    async def test_disabled_module_is_absent_from_entitled_features(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        """The MCP tool-list source (`entitled_features`) never sees a disabled Module's contracts."""
        from flask_core.feature_registry import entitled_features

        feature_registry, app_registry = registries
        load_enabled_modules(
            enabled={"bot", "social", "marketing"},  # customer omitted
            feature_registry=feature_registry,
            app_registry=app_registry,
        )

        async def _always_true(*_args: object, **_kwargs: object) -> bool:
            return True

        result = await entitled_features(
            tenant="acme",
            contracts=feature_registry.all_contracts(),
            check=_always_true,
        )

        assert not any(c.module == "customer" for c in result)
        assert any(c.module == "bot" for c in result)


class TestZeroModulesEnabled:
    def test_explicit_empty_set_registers_only_core_platform(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        results = load_enabled_modules(
            enabled=set(),
            feature_registry=feature_registry,
            app_registry=app_registry,
        )

        assert set(results) == {"core_platform"}
        assert len(feature_registry.all_contracts()) == EXPECTED_COUNTS["core_platform"]
        for module in TOGGLEABLE_MODULES:
            assert feature_registry.for_module(module) == ()


class TestUnknownModuleRejected:
    def test_unknown_label_in_enabled_raises_value_error(
        self, registries: tuple[FeatureRegistry, AppRegistry]
    ) -> None:
        feature_registry, app_registry = registries
        with pytest.raises(ValueError, match="not a toggleable module"):
            load_enabled_modules(
                enabled={"bot", "not_a_real_module"},
                feature_registry=feature_registry,
                app_registry=app_registry,
            )


class TestEnabledModulesFromEnv:
    """Env-var parsing in isolation, independent of registration."""

    def test_present_true_is_enabled(self) -> None:
        assert enabled_modules_from_env({"MODULE_LOAD_BOT": "true"}) == {"bot"}

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", "enabled"])
    def test_various_truthy_spellings_are_enabled(self, value: str) -> None:
        assert enabled_modules_from_env({"MODULE_LOAD_SOCIAL": value}) == {"social"}

    @pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", ""])
    def test_various_falsy_spellings_are_disabled(self, value: str) -> None:
        assert enabled_modules_from_env({"MODULE_LOAD_MARKETING": value}) == frozenset()

    def test_absent_var_is_disabled(self) -> None:
        assert enabled_modules_from_env({}) == frozenset()

    def test_all_four_present_matches_helm_template_shape(self) -> None:
        env = {
            "MODULE_LOAD_BOT": "true",
            "MODULE_LOAD_SOCIAL": "true",
            "MODULE_LOAD_MARKETING": "true",
            "MODULE_LOAD_CUSTOMER": "true",
        }
        assert enabled_modules_from_env(env) == frozenset(TOGGLEABLE_MODULES)

    def test_irrelevant_env_vars_are_ignored(self) -> None:
        env = {"MODULE_LOAD_BOT": "true", "SOME_OTHER_VAR": "true", "MODULE_PORT": "8080"}
        assert enabled_modules_from_env(env) == {"bot"}
