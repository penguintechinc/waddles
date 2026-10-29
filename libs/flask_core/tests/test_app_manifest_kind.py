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
def test_absent_kind_never_inferred_service(module: str) -> None:
    data = {
        **_BASE,
        "module": module,
        "feature": f"waddles.{module}.x",
        "app_id": f"waddles.{module}.x.y",
    }
    assert parse_manifest(data).kind == "app"


def test_core_namespace_and_integrations_module_default_app() -> None:
    core = {**_BASE, "app_id": "waddles.core.x.y", "feature": "waddles.core.x"}
    assert parse_manifest(core).kind == "app"
    assert (
        parse_manifest(
            {
                **_BASE,
                "module": "integrations",
                "feature": "waddles.integrations.x",
                "app_id": "waddles.integrations.x.y",
            }
        ).kind
        == "app"
    )


def test_invalid_kind_rejected() -> None:
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest({**_BASE, "kind": "plugin"})
    assert exc_info.value.reason == REASON_INVALID_KIND
