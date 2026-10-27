"""Tests for `waddle_sdk.flask_core.feature_flags`."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from waddle_sdk.flask_core.feature_flags import feature_enabled, tier, tier_at_least


def test_feature_enabled_falls_back_to_default_outside_a_component() -> None:
    """No wit_world module is importable in a host-side pytest run -- degrade to default."""
    assert asyncio.run(feature_enabled("waddles.core.example", tenant="acme", default=True)) is True
    assert (
        asyncio.run(feature_enabled("waddles.core.example", tenant="acme", default=False)) is False
    )


def test_feature_enabled_uses_the_wit_import_when_available(monkeypatch) -> None:
    """When `wit_world` is importable, the WIT `flags.enabled` import answers, not the fallback."""

    def fake_enabled(key: str, default_value: bool) -> bool:
        assert key == "waddles.core.example"
        return not default_value  # prove the WIT path, not the fallback, answered

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(enabled=fake_enabled)
    )  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    result = asyncio.run(
        feature_enabled("waddles.core.example", tenant="acme", community=1, default=False)
    )
    assert result is True


def test_feature_enabled_degrades_when_wit_world_has_no_flags_attribute(monkeypatch) -> None:
    """A component wizened against an older world (no `flags` import) must never crash.

    Regression for the live-alpha bug: `wit_world` imports fine (the
    component instantiated), but `wit_world.imports` has no `flags`
    attribute -- previously an uncaught `AttributeError` that propagated out
    of `transform()` as a wasm trap (`unreachable`), dropping the reply
    entirely. Must fall back to `default`, matching the `ImportError` path.
    """
    fake_wit_world = types.ModuleType("wit_world")
    # `imports` deliberately has no `flags` attribute -- mirrors a stale
    # component built before the WIT world declared `%flags`.
    fake_wit_world.imports = types.SimpleNamespace()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert (
        asyncio.run(feature_enabled("waddles.core.example", tenant="acme", default=True)) is True
    )
    assert (
        asyncio.run(feature_enabled("waddles.core.example", tenant="acme", default=False)) is False
    )


def test_feature_enabled_discards_tenant_and_community_for_call_site_compatibility(
    monkeypatch,
) -> None:
    """`tenant`/`community` match the real call site but never reach the WIT import."""
    captured: dict = {}

    def fake_enabled(key: str, default_value: bool) -> bool:
        captured["args"] = (key, default_value)
        return default_value

    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(enabled=fake_enabled)
    )  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    asyncio.run(
        feature_enabled("waddles.bot.command_aliases", tenant="acme", community=1, default=True)
    )
    assert captured["args"] == ("waddles.bot.command_aliases", True)


def test_tier_falls_back_to_free_outside_a_component() -> None:
    """No wit_world module is importable in a host-side pytest run -- degrade to free."""
    assert asyncio.run(tier()) == "free"


def test_tier_uses_the_wit_import_when_available(monkeypatch) -> None:
    """When `wit_world` is importable, the WIT `flags.tier` import answers, not the fallback."""
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(tier=lambda: "enterprise")
    )  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert asyncio.run(tier()) == "enterprise"


def test_tier_degrades_when_wit_world_has_no_flags_attribute(monkeypatch) -> None:
    """A component wizened against an older world (no `flags` import) must never crash."""
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert asyncio.run(tier()) == "free"


def test_tier_degrades_to_free_on_unrecognized_tier_string(monkeypatch) -> None:
    """A host/SDK skew reporting an unknown tier string fails open to free, never crashes."""
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(tier=lambda: "gold")
    )  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert asyncio.run(tier()) == "free"


def test_tier_at_least_compares_ordinal_rank(monkeypatch) -> None:
    """tier_at_least() compares the tenant's tier against the required tier's rank."""
    fake_wit_world = types.ModuleType("wit_world")
    fake_wit_world.imports = types.SimpleNamespace(
        flags=types.SimpleNamespace(tier=lambda: "professional")
    )  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wit_world", fake_wit_world)

    assert asyncio.run(tier_at_least("free")) is True
    assert asyncio.run(tier_at_least("professional")) is True
    assert asyncio.run(tier_at_least("enterprise")) is False


def test_tier_at_least_rejects_an_unknown_required_tier() -> None:
    """A typo'd `required` tier is a bug to surface immediately, not fail open on."""
    with pytest.raises(ValueError, match="unknown license tier"):
        asyncio.run(tier_at_least("gold"))
