"""Manifest regression: `define` stays inside the platform's bundle limit ceilings.

The hub-api manifest parser rejects `limits.timeout_ms` above 10000 (`limit_out_of_range`); this
bundle declared 15000, which made the core-bundle seeder refuse it. Both manifest files must
sit inside the ceilings and agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

BUNDLE_DIR = Path(__file__).resolve().parent.parent
MANIFESTS = ("bundle.yaml", "hub-manifest.yaml")
MAX_TIMEOUT_MS = 10_000  # hub_api/services/bundle_manifest_v2.py::_MAX_TIMEOUT_MS


def _limits(manifest: str) -> dict[str, int]:
    """The `limits:` block of one of the bundle's manifest files."""
    raw = yaml.safe_load((BUNDLE_DIR / manifest).read_text(encoding="utf-8"))
    limits = raw["limits"]
    assert isinstance(limits, dict)
    return limits


@pytest.mark.parametrize("manifest", MANIFESTS)
def test_timeout_is_within_the_platform_ceiling(manifest: str) -> None:
    """`timeout_ms` is within the parser's accepted 50..=10000 range."""
    assert 50 <= _limits(manifest)["timeout_ms"] <= MAX_TIMEOUT_MS


def test_both_manifest_files_declare_the_same_timeout() -> None:
    """`bundle.yaml` and `hub-manifest.yaml` mirror each other (the seeder reads the latter)."""
    assert len({_limits(m)["timeout_ms"] for m in MANIFESTS}) == 1
