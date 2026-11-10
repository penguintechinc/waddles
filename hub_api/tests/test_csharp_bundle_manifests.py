"""Every C# bundle's `hub-manifest.yaml` must parse under hub-api's v2 manifest parser.

`bundle.yaml` (SDK compiler schema: `artifact: component`) is a different consumer and is
deliberately NOT fed to `parse_bundle_manifest_v2`; `hub-manifest.yaml` is what the core
seeder feeds it. Regression: three SuperPenguin manifests shipped without `schema_version`.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

from services.bundle_manifest_v2 import ManifestV2Error, parse_bundle_manifest_v2

_CSHARP_DIR = pathlib.Path(__file__).resolve().parents[2] / "bundles" / "csharp"
_HUB_MANIFESTS = sorted(_CSHARP_DIR.glob("*/hub-manifest.yaml"))


def _load(path: pathlib.Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{path} is not a mapping"
    return raw


def _parse(raw: dict[str, Any]) -> Any:
    return parse_bundle_manifest_v2(
        raw,
        known_custom_platforms=frozenset(),
        allow_wildcard_consumes=False,
        allow_prebuilt=True,
        trusted_source=True,
    )


def test_csharp_manifests_were_discovered() -> None:
    """A zero-item scan is a failure, not a pass."""
    assert len(_HUB_MANIFESTS) >= 1, f"no hub-manifest.yaml found under {_CSHARP_DIR}"


@pytest.mark.parametrize("path", _HUB_MANIFESTS, ids=lambda p: p.parent.name)
def test_hub_manifest_parses_v2(path: pathlib.Path) -> None:
    """The hub-api install gate accepts the manifest (trusted seeder path)."""
    assert _parse(_load(path)).app_id


@pytest.mark.parametrize("path", _HUB_MANIFESTS, ids=lambda p: p.parent.name)
def test_sdk_bundle_yaml_stays_component_schema(path: pathlib.Path) -> None:
    """`bundle.yaml` is the SDK schema: it keeps `artifact: component`, never the hub schema."""
    sdk = _load(path.parent / "bundle.yaml")
    assert sdk["artifact"] == "component"
    with pytest.raises(ManifestV2Error, match="invalid_provider"):
        _parse(sdk)
