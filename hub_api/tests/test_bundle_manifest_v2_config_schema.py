"""Per-community config schema: legacy `config_schema` deprecation + the seven migrated bundles."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2
from tests.test_bundle_manifest_v2 import _parse

_REPO = Path(__file__).resolve().parents[2]
_SCHEMA = {"type": "object", "properties": {"x": {"type": "string", "default": "a"}}}

#: (manifest file, app_id) of every bundle whose schema was moved off the dead legacy key.
_MIGRATED = [
    ("bundles/rust/calendar", "waddles.core.app.calendar"),
    ("bundles/rust/ai-teammates", "waddles.core.example.ai-teammates"),
    ("bundles/rust/game-servers", "waddles.core.example.game-servers"),
    ("bundles/rust/rooms", "waddles.core.app.rooms"),
    ("bundles/rust/twitch-clips", "waddles.core.example.twitch-clips"),
    ("bundles/rust/live-now", "waddles.core.app.live-now"),
    ("bundles/python/secret", "waddles.core.example.secret"),
]


def test_non_empty_legacy_config_schema_is_rejected() -> None:
    with pytest.raises(ManifestV2Error) as exc:
        _parse({"config_schema": _SCHEMA})
    assert exc.value.reason == "deprecated_config_schema"


def test_empty_legacy_config_schema_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="services.bundle_manifest_v2"):
        parsed = _parse({"config_schema": {}})
    assert parsed.community_config_schema is None  # type: ignore[attr-defined]
    assert any("deprecated `config_schema`" in r.getMessage() for r in caplog.records)


def test_community_config_schema_alone_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="services.bundle_manifest_v2"):
        parsed = _parse({"community_config_schema": _SCHEMA})
    assert parsed.community_config_schema == _SCHEMA  # type: ignore[attr-defined]
    assert not [r for r in caplog.records if "config_schema" in r.getMessage()]


def _walk_defaults(schema: dict[str, Any], path: str, missing: list[str]) -> None:
    for name, sub in (schema.get("properties") or {}).items():
        here = f"{path}/{name}"
        if "default" not in sub and name not in set(schema.get("required") or []):
            missing.append(here)
        _walk_defaults(sub, here, missing)


@pytest.mark.parametrize("manifest_file", ["hub-manifest.yaml", "bundle.yaml"])
@pytest.mark.parametrize(("directory", "app_id"), _MIGRATED)
def test_migrated_manifest_parses_community_config_schema_with_defaults(
    directory: str, app_id: str, manifest_file: str
) -> None:
    path = _REPO / directory / manifest_file
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "config_schema" not in raw
    assert raw["app_id"] == app_id
    if manifest_file == "bundle.yaml":
        # bundle.yaml is the crate-side copy (other consumer's schema); it must carry the
        # same schema, and that schema must itself be valid.
        hub = yaml.safe_load((_REPO / directory / "hub-manifest.yaml").read_text("utf-8"))
        assert raw["community_config_schema"] == hub["community_config_schema"]
        from services.bundle_manifest_v2 import parse_config_schema

        schema = parse_config_schema(raw["community_config_schema"])
    else:
        parsed = parse_bundle_manifest_v2(
            raw,
            known_custom_platforms=frozenset(),
            allow_wildcard_consumes=False,
            allow_prebuilt=True,
            trusted_source=True,
        )
        schema = parsed.community_config_schema
    assert schema is not None and schema["type"] == "object" and schema["properties"]
    missing: list[str] = []
    _walk_defaults(schema, "", missing)
    assert missing == []
