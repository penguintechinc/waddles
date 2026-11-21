"""Tests for the optional `kind` ("app" | "service") manifest field."""

from __future__ import annotations

from flask_core.app_manifest import (
    REASON_INVALID_KIND,
    ManifestError,
    parse_manifest,
)

import pytest

_BASE: dict[str, object] = {
    "app_id": "penguintechinc.waddles.gateway",
    "name": "Discord Gateway Ingest",
    "version": "1.0.0",
    "feature": "discord",
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
        "feature": "x",
        "app_id": f"penguintechinc.waddles.x-{module}",
    }
    assert parse_manifest(data).kind == "app"


def test_core_namespace_and_integrations_module_default_app() -> None:
    core = {**_BASE, "app_id": "penguintechinc.waddles.y", "feature": "x"}
    assert parse_manifest(core).kind == "app"
    assert (
        parse_manifest(
            {
                **_BASE,
                "module": "integrations",
                "feature": "x",
                "app_id": "acmecorp.widgets.y",
            }
        ).kind
        == "app"
    )


def test_invalid_kind_rejected() -> None:
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest({**_BASE, "kind": "plugin"})
    assert exc_info.value.reason == REASON_INVALID_KIND


@pytest.mark.parametrize(
    "app_id",
    [
        "waddles.bot.discord.gateway",
        "penguintechinc.waddles",
        "penguintechinc.waddles.a.b",
        "Penguintechinc.waddles.x",
    ],
)
def test_app_id_must_be_three_lowercase_segments(app_id: str) -> None:
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest({**_BASE, "app_id": app_id})
    assert exc_info.value.reason == "not_namespaced"


def test_feature_is_optional_and_unrelated_to_app_id() -> None:
    no_feature = {k: v for k, v in _BASE.items() if k != "feature"}
    assert parse_manifest(no_feature).feature == "penguintechinc.waddles"
    assert parse_manifest({**_BASE, "feature": "anything.descriptive"}).feature == (
        "anything.descriptive"
    )
    with pytest.raises(ManifestError):
        parse_manifest({**_BASE, "feature": "Not Valid"})
