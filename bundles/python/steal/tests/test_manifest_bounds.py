"""Manifest regression: `steal` declares the per-call economy bounds the host enforces.

The hub-api manifest parser rejects `economy.wager` / `economy.transfer` without an integer
`params.max_bet` / `params.max_amount` in 1..=1000 (`invalid_economy_bound`), which made the
core-bundle seeder refuse the whole bundle. The declared bound is also the enforced ceiling, so it
must cover what the bundle actually stakes / pays (`STAKE`) and both manifest files must agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app import STAKE

BUNDLE_DIR = Path(__file__).resolve().parent.parent
MANIFESTS = ("bundle.yaml", "hub-manifest.yaml")


def _params(manifest: str, permission_id: str) -> dict[str, object]:
    """The `params` of one declared permission in one of the bundle's manifest files."""
    raw = yaml.safe_load((BUNDLE_DIR / manifest).read_text(encoding="utf-8"))
    matches = [p for p in raw["permissions"] if p["id"] == permission_id]
    assert len(matches) == 1, f"{manifest}: {permission_id} declared {len(matches)} times"
    params = matches[0].get("params")
    assert isinstance(params, dict), f"{manifest}: {permission_id} declares no params"
    return params


@pytest.mark.parametrize("manifest", MANIFESTS)
@pytest.mark.parametrize(
    ("permission_id", "param"), [("economy.wager", "max_bet"), ("economy.transfer", "max_amount")]
)
def test_declared_economy_bound_is_valid_and_covers_the_stake(
    manifest: str, permission_id: str, param: str
) -> None:
    """The bound is an int in 1..=1000 (parser rule) and at least the fixed `STAKE`."""
    bound = _params(manifest, permission_id)[param]
    assert isinstance(bound, int) and not isinstance(bound, bool)
    assert 1 <= bound <= 1000
    assert bound >= STAKE, "the host would refuse the bundle's own stake / penalty"


@pytest.mark.parametrize(
    ("permission_id", "param"), [("economy.wager", "max_bet"), ("economy.transfer", "max_amount")]
)
def test_both_manifest_files_declare_the_same_bound(permission_id: str, param: str) -> None:
    """`bundle.yaml` and `hub-manifest.yaml` mirror each other (the seeder reads the latter)."""
    declared = {_params(m, permission_id)[param] for m in MANIFESTS}
    assert len(declared) == 1, f"manifests disagree on {permission_id}.{param}: {declared}"
