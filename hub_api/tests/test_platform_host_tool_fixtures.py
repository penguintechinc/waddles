"""Drift gate: the Rust host-tool planner fixtures must equal the real platform manifests.

`core/svc_action/tests/fixtures/host_tools/<platform>.json` is the consent summary of each
platform service bundle's `exported_tools` (incl. `host_op` + `required_scope`); the Rust
planner tests load it so they exercise the REAL declarations. This test regenerates the
summary from `bundle.yaml` AND `hub-manifest.yaml` through the production parser and fails
when either differs from the checked-in fixture (regenerate: `scripts/host_tool_summary.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from services.bundle_manifest_v2 import parse_exported_tools
from services.permission_summary_service import _exported_tool_summary

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = sorted((ROOT / "core/svc_action/tests/fixtures/host_tools").glob("*.json"))


def _summary(manifest: Path) -> list[dict]:
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    tools = parse_exported_tools(raw.get("exported_tools"))
    return sorted((_exported_tool_summary(t) for t in tools), key=lambda e: e["name"])


def _bundle_dir(platform: str) -> Path:
    found = sorted(p.parent for p in (ROOT / "bundles").glob(f"*/{platform}/bundle.yaml"))
    assert len(found) == 1, f"expected exactly one bundle dir for {platform}, got {found}"
    return found[0]


def test_fixtures_exist() -> None:
    """Zero fixtures examined would make the drift gate vacuous."""
    assert FIXTURES, "no host-tool fixtures found"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
@pytest.mark.parametrize("manifest_name", ["bundle.yaml", "hub-manifest.yaml"])
def test_fixture_matches_manifest(fixture: Path, manifest_name: str) -> None:
    """Both manifests of the platform bundle produce exactly the checked-in summary."""
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    actual = _summary(_bundle_dir(fixture.stem) / manifest_name)
    assert actual, "no exported tools parsed"
    assert actual == expected, f"{fixture.name} is stale: run scripts/host_tool_summary.py --write"
    assert any(t.get("hostOp") for t in actual), "platform bundle declares no host_op tool"
