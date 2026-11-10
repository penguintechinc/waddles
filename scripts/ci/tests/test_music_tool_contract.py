"""Music provider bundles' exported_tools must equal the shared contract-derived block."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "gen_music", ROOT / "scripts/gen-music-manifest-tools.py"
)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

PROVIDERS = [p.parent.name.removeprefix("music-") for p in (ROOT / "bundles/rust").glob("music-*/bundle.yaml")]


def test_at_least_one_provider_examined() -> None:
    """Zero examined bundles is a failure, not a pass."""
    assert PROVIDERS, "no music-* bundles found"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("name", ["bundle.yaml", "hub-manifest.yaml"])
def test_manifest_matches_contract(provider: str, name: str) -> None:
    """Each manifest's exported_tools equals the contract block (full or a declared subset)."""
    doc = yaml.safe_load((ROOT / f"bundles/rust/music-{provider}/{name}").read_text())
    got = doc["exported_tools"]
    prefix = f"music.{provider}."
    subset = [t["name"].removeprefix(prefix) for t in got]
    assert gen.build(provider, subset) == got


def test_generator_detects_drift() -> None:
    """The comparison must be able to fail."""
    expected = gen.build("youtube", [])
    mutated = [dict(t) for t in expected]
    mutated[0]["risk"] = "dangerous"
    assert mutated != expected
