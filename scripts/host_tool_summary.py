#!/usr/bin/env python3
"""Emit the consent-summary `exportedTools` JSON of a platform bundle manifest.

Usage: host_tool_summary.py <bundle-dir> [--write <fixture.json>] [--check <fixture.json>]

The Rust host-tool planner tests (`core/svc_action`) consume the SAME summary shape the hub
approval pipeline stores, so they drive the REAL manifest declarations (`host_op`,
`required_scope`) end to end instead of hand-built copies. `--check` fails loud when the
checked-in fixture drifts from the manifest; `--write` regenerates it. Both `bundle.yaml`
and `hub-manifest.yaml` must agree, or the run fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hub_api"))

from services.bundle_manifest_v2 import parse_exported_tools  # noqa: E402
from services.permission_summary_service import _exported_tool_summary  # noqa: E402


def summarize(manifest: Path) -> list[dict]:
    """Return the sorted consent-summary entries of `manifest`'s exported tools."""
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    tools = parse_exported_tools(raw.get("exported_tools"))
    if not tools:
        sys.exit(f"{manifest}: no exported_tools parsed (zero examined is a failure)")
    return sorted((_exported_tool_summary(t) for t in tools), key=lambda e: e["name"])


def main(argv: list[str]) -> int:
    """CLI entry: summarise both manifests, require agreement, then write/check a fixture."""
    if not argv:
        sys.exit(__doc__)
    bundle = Path(argv[0])
    a = summarize(bundle / "bundle.yaml")
    b = summarize(bundle / "hub-manifest.yaml")
    if a != b:
        sys.exit(f"{bundle}: bundle.yaml and hub-manifest.yaml exported_tools disagree")
    text = json.dumps(a, indent=2, sort_keys=True) + "\n"
    if "--write" in argv:
        Path(argv[argv.index("--write") + 1]).write_text(text, encoding="utf-8")
    if "--check" in argv:
        fixture = Path(argv[argv.index("--check") + 1])
        if fixture.read_text(encoding="utf-8") != text:
            sys.exit(f"{fixture} is stale: rerun with --write")
    print(f"{bundle}: {len(a)} exported tools ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
