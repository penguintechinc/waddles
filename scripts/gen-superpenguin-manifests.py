#!/usr/bin/env python3
"""Generate bundle.yaml and hub-manifest.yaml of a SuperPenguinTV C# app bundle from its compact `manifest.src.yaml`.

The two manifests must stay byte-for-byte consistent in everything but the build-artifact fields, so they are generated, never
hand-edited. Usage: python3 -I scripts/gen-superpenguin-manifests.py <slug> [<slug> ...]   (make gen-superpenguin-manifests)

`manifest.src.yaml` keys: slug, title, version, upstream (list of upstream source paths for the notice), consumes (list of
{platform, event_types, filters?}), permissions, requested_tools, limits (optional), overlay_origins (optional map of overlay/ file -> upstream path or `original`; every file under overlay/ must be listed), extra
(optional dict merged into both manifests). Every other field (author github/psychoboy, collection superpenguintv, MIT notice,
category alternatives, stage entries) is fixed here so no port can drift from the attribution contract.
"""

from __future__ import annotations

import hashlib
import mimetypes
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUNDLES = ROOT / "bundles" / "csharp"
NOTICE_TEMPLATE = """Ported from PenguinTwitchBot ({sources}, github.com/Psychoboy/
PenguinTwitchBot) by superpenguintv (Psychoboy), used with permission.

MIT License

Copyright (c) 2023 Psychoboy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


class _Dumper(yaml.SafeDumper):
    """SafeDumper that writes multi-line strings as literal blocks (the notice)."""


def _str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str)


def overlay_assets(directory: Path, origins: dict[str, str]) -> list[dict]:
    """Declare every shipped file under `<bundle>/overlay/` (sha256, type, origin) as a manifest overlay-asset entry.

    `origins` maps a path relative to `overlay/` to its upstream source path, or `original` for files written for Waddles; a
    file with no declared origin is a failure so nothing of unknown provenance can ship. Files whose name starts with `_` or
    ends with `.md` are documentation, not assets.
    """
    root = directory / "overlay"
    entries: list[dict] = []
    if not root.is_dir():
        return entries
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith("_") and p.suffix != ".md"):
        rel = path.relative_to(root).as_posix()
        if rel not in origins:
            raise SystemExit(f"gen-superpenguin-manifests: FAIL -- overlay/{rel} has no declared origin in manifest.src.yaml")
        content_type = {".js": "text/javascript", ".svg": "image/svg+xml"}.get(path.suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        entries.append(
            {
                "path": rel,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content_type": content_type,
                "origin": origins[rel],
                "license": "MIT",
            }
        )
    return entries


def build(src: dict, *, install: bool, assets: list[dict] | None = None) -> dict:
    """Build one manifest (`install=True` -> hub-manifest.yaml shape, else bundle.yaml shape)."""
    slug = src["slug"]
    process: dict = {"entry": "waddle:bundle/process-stage#transform", "consumes": src["consumes"]}
    action: dict = {"entry": "waddle:bundle/action-stage#dispatch"}
    if not install:
        process.update({"produces": [], "config": {}, "spec": {"required_config": []}})
        action.update({"config": {}, "spec": {"required_config": []}})
    manifest: dict = {
        "schema_version": 2,
        "app_id": f"waddles.integrations.superpenguin.{slug}",
        "name": src["title"],
        "version": src["version"],
        "feature": "waddles.integrations.superpenguin",
        "module": "integrations",
        "provider": "thirdparty",
        "language": "csharp",
        "artifact": "prebuilt" if install else "component",
        "execution_model": "native" if install else "wasi-component",
        "is_default": False,
        "author": "github/psychoboy",
        "license": "MIT",
        "category": "alternatives",
        "source_url": "https://github.com/Psychoboy/PenguinTwitchBot",
        "notice": NOTICE_TEMPLATE.format(sources=", ".join(src["upstream"])),
        "stages": {"process": process, "action": action},
        "egress": [],
        "data": {"tables": []},
        "limits": src.get("limits", {"timeout_ms": 2000, "memory_mb": 64, "egress_rps": 10}),
        "permissions": src["permissions"],
    }
    if install:
        manifest["routes_to"] = []
    else:
        manifest.update(
            {
                "config_schema": {},
                "compatible_with": [],
                "incompatible_with": [],
                "platform_compatibility": {"tested_with": "1.0.0", "min_version": "1.0.0", "max_version": None},
            }
        )
    manifest["requested_tools"] = src["requested_tools"]
    manifest["collection"] = "superpenguintv"
    if assets:
        manifest["overlay_assets"] = assets
    manifest.update(src.get("extra", {}))
    return manifest


def main(argv: list[str]) -> int:
    """Generate both manifests for every slug; a missing source or an empty slug list is a failure."""
    if not argv:
        print("gen-superpenguin-manifests: FAIL -- no bundle slug given", file=sys.stderr)
        return 1
    for slug in argv:
        directory = BUNDLES / f"superpenguin-{slug}"
        source = directory / "manifest.src.yaml"
        if not source.is_file():
            print(f"gen-superpenguin-manifests: FAIL -- {source} missing", file=sys.stderr)
            return 1
        src = yaml.safe_load(source.read_text())
        if src["slug"] != slug:
            print(f"gen-superpenguin-manifests: FAIL -- slug mismatch in {source}", file=sys.stderr)
            return 1
        assets = overlay_assets(directory, src.get("overlay_origins", {}))
        for name, install in (("bundle.yaml", False), ("hub-manifest.yaml", True)):
            text = yaml.dump(build(src, install=install, assets=assets), Dumper=_Dumper, sort_keys=False, width=100, allow_unicode=True)
            (directory / name).write_text(text)
        print(f"gen-superpenguin-manifests: wrote {directory}/bundle.yaml + hub-manifest.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
