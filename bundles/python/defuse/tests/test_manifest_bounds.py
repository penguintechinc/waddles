"""Manifest regression: `defuse` declares the per-call `economy.wager` bound the host enforces.

The hub-api manifest parser rejects `economy.wager` without an integer `params.max_bet` in
1..=1000 (`invalid_economy_bound`), which made the core-bundle seeder refuse the whole bundle. The
declared bound is also what `!defuse all` stakes (the host's `max_bet`), so it must cover the
default stake and both manifest files must agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app import DEFAULT_STAKE

BUNDLE_DIR = Path(__file__).resolve().parent.parent
MANIFESTS = ("bundle.yaml", "hub-manifest.yaml")


def _max_bet(manifest: str) -> object:
    """The declared `economy.wager` `params.max_bet` in one of the bundle's manifest files."""
    raw = yaml.safe_load((BUNDLE_DIR / manifest).read_text(encoding="utf-8"))
    matches = [p for p in raw["permissions"] if p["id"] == "economy.wager"]
    assert len(matches) == 1, f"{manifest}: economy.wager declared {len(matches)} times"
    params = matches[0].get("params")
    assert isinstance(params, dict), f"{manifest}: economy.wager declares no params"
    return params["max_bet"]


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_declared_max_bet_is_valid_and_covers_the_default_stake(manifest: str) -> None:
    """`max_bet` is an int in 1..=1000 (parser rule) and at least the default stake."""
    bound = _max_bet(manifest)
    assert isinstance(bound, int) and not isinstance(bound, bool)
    assert 1 <= bound <= 1000
    assert bound >= DEFAULT_STAKE


def test_both_manifest_files_declare_the_same_max_bet() -> None:
    """`bundle.yaml` and `hub-manifest.yaml` mirror each other (the seeder reads the latter)."""
    assert len({_max_bet(m) for m in MANIFESTS}) == 1
