#!/usr/bin/env python3
"""Emit the `exported_tools:` YAML block for a music provider bundle from the shared contract.

Usage: gen-music-manifest-tools.py <provider> [tool ...] [--check <manifest.yaml> ...]

Reads bundles/contracts/music-tool-contract.v1.json, inlines its `$ref`s (manifest schemas
carry no refs), prefixes each tool `music.<provider>.` and prints the block. With `--check`,
fails (non-zero) unless each given manifest's contract tools equal the generated block --
so bundle.yaml / hub-manifest.yaml can never drift from the contract. Tools whose suffix is not a
contract tool (e.g. Spotify Connect `player.*`, behind `capability.playback: connect`) are
provider extensions: they are exempt from the comparison but must follow the contract tools.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parent.parent / "bundles/contracts/music-tool-contract.v1.json"


def _inline(node, defs):
    """Recursively replaces `{"$ref": "#/definitions/x"}` with the definition body."""
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline(defs[node["$ref"].rsplit("/", 1)[1]], defs)
        return {k: _inline(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def build(provider: str, only: list[str]) -> list[dict]:
    """Returns the manifest `exported_tools` list for `provider` (all contract tools by default)."""
    contract = json.loads(CONTRACT.read_text())
    tools = contract["tools"]
    names = only or list(tools)
    unknown = [n for n in names if n not in tools]
    if unknown:
        raise SystemExit(f"unknown contract tools: {unknown}")
    out = []
    for n in names:
        t = _inline(tools[n], contract["definitions"])
        out.append({"name": f"music.{provider}.{n}", **t})
    return out


def split(provider: str, got: list[dict]) -> tuple[list[dict], list[dict]]:
    """Splits a manifest's exported_tools into (contract tools, provider extensions).

    Raises SystemExit when a tool lacks the `music.<provider>.` prefix or an extension precedes a
    contract tool, so extensions can never shadow or reorder the portable surface.
    """
    tools = json.loads(CONTRACT.read_text())["tools"]
    prefix = f"music.{provider}."
    common: list[dict] = []
    ext: list[dict] = []
    for t in got:
        name = t.get("name", "")
        if not name.startswith(prefix):
            raise SystemExit(f"tool {name!r} is not prefixed {prefix!r}")
        if name.removeprefix(prefix) in tools:
            if ext:
                raise SystemExit(f"contract tool {name} follows a provider extension")
            common.append(t)
        else:
            ext.append(t)
    return common, ext


class _NoAlias(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def main(argv: list[str]) -> int:
    """CLI entry: print the block, or verify manifests with --check."""
    if "--check" in argv:
        i = argv.index("--check")
        args, manifests = argv[:i], argv[i + 1 :]
    else:
        args, manifests = argv, []
    if not args:
        raise SystemExit(__doc__)
    expected = build(args[0], args[1:])
    if not manifests:
        sys.stdout.write(
            yaml.dump({"exported_tools": expected}, Dumper=_NoAlias, sort_keys=False)
        )
        return 0
    bad = 0
    for m in manifests:
        got, _ext = split(args[0], yaml.safe_load(Path(m).read_text()).get("exported_tools") or [])
        if got != expected:
            print(f"DRIFT: {m} exported_tools != contract-derived block", file=sys.stderr)
            bad += 1
        else:
            print(f"ok: {m} ({len(expected)} tools)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
