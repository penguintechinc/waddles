"""Tests for `waddle_sdk.flask_core.feature_flags`."""

from __future__ import annotations

import asyncio
import sys
import types

from waddle_sdk.flask_core.feature_flags import feature_enabled


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
