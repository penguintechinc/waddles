#!/usr/bin/env python3
"""Emit the `exported_tools:` YAML entry for a platform bundle from the live-channels contract.

Usage: gen-live-manifest-tools.py <platform> [--check <manifest.yaml> ...]

Reads bundles/contracts/live-channels-contract.v1.json, inlines its `$ref`s (manifest schemas
carry no refs), names the tool `<platform>.live.channels` and prints the entry. With `--check`,
fails (non-zero) unless each given manifest's `exported_tools` CONTAINS the generated entry
verbatim (a platform bundle exports other tools too) -- so a platform bundle's manifest can
never drift from the contract the `live-now` app consumes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parent.parent / "bundles/contracts/live-channels-contract.v1.json"


def _inline(node, defs):
    """Recursively replaces `{"$ref": "#/definitions/x"}` with the definition body."""
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(defs[node["$ref"].rsplit("/", 1)[1]], defs)
        return {k: _inline(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def build(platform: str) -> dict:
    """Returns the manifest `exported_tools` entry for `platform`."""
    contract = json.loads(CONTRACT.read_text())
    if platform not in contract["platforms"]:
        raise SystemExit(f"unknown platform {platform!r}; contract platforms: {contract['platforms']}")
    tool = _inline(contract["tools"]["channels"], contract["definitions"])
    return {"name": contract["tool_name_template"].format(platform=platform), **tool}


class _NoAlias(yaml.SafeDumper):
    def ignore_aliases(self, data):  # noqa: D401 - PyYAML hook
        return True


def main(argv: list[str]) -> int:
    """CLI entry: print the entry, or verify manifests with --check."""
    if "--check" in argv:
        i = argv.index("--check")
        args, manifests = argv[:i], argv[i + 1 :]
    else:
        args, manifests = argv, []
    if len(args) != 1:
        raise SystemExit(__doc__)
    expected = build(args[0])
    if not manifests:
        sys.stdout.write(yaml.dump({"exported_tools": [expected]}, Dumper=_NoAlias, sort_keys=False))
        return 0
    bad = 0
    for m in manifests:
        got = yaml.safe_load(Path(m).read_text()).get("exported_tools") or []
        if expected not in got:
            print(f"DRIFT: {m} lacks the contract-derived {expected['name']} entry", file=sys.stderr)
            bad += 1
        else:
            print(f"ok: {m} ({expected['name']})")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
