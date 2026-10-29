"""Tests for the optional `kind` ("app" | "service") manifest field."""

from __future__ import annotations

from flask_core.app_manifest import (
    REASON_INVALID_KIND,
    ManifestError,
    parse_manifest,
)

import pytest

_BASE: dict[str, object] = {
    "app_id": "waddles.bot.discord.gateway",
    "name": "Discord Gateway Ingest",
    "version": "1.0.0",
    "feature": "waddles.bot.discord",
    "module": "bot",
    "provider": "builtin",
}


def test_explicit_kind_is_preserved() -> None:
    assert parse_manifest({**_BASE, "kind": "service"}).kind == "service"
    assert parse_manifest({**_BASE, "kind": "app"}).kind == "app"


def test_absent_kind_inferred_app() -> None:
    assert parse_manifest(dict(_BASE)).kind == "app"


@pytest.mark.parametrize("module", ["core", "integrations"])
def test_absent_kind_inferred_service_for_platform_module(module: str) -> None:
    data = {
        **_BASE,
        "module": module,
        "feature": f"waddles.{module}.x",
        "app_id": f"waddles.{module}.x.y",
    }
    assert parse_manifest(data).kind == "service"


def test_invalid_kind_rejected() -> None:
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest({**_BASE, "kind": "plugin"})
    assert exc_info.value.reason == REASON_INVALID_KIND
