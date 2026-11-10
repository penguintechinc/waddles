#!/usr/bin/env python3
"""Writes <overlay-dir>/ASSETS.json for a drop-game bundle's vendored overlay files.

Every file under the overlay dir (except ASSETS.json and SCHEMA.md) is listed with its sha256, size, upstream path
(`PenguinTwitchBot/wwwroot/<relative path>`, with an optional leading directory stripped) and the upstream commit it was
copied from. Material that credits another source, has its own license or has no clear license is passed in
--unclear (`path|reason`, repeatable) and is listed under `third_party_or_unclear` but never vendored.

Usage: gen_assets.py <overlay-dir> --commit <sha> [--strip common/] [--unclear 'js/x.js|reason' ...] [--note 'text' ...]
"""
import argparse
import hashlib
import json
import pathlib
import sys

LICENSE = "MIT (Psychoboy/PenguinTwitchBot LICENSE)"
SKIP = {"ASSETS.json", "SCHEMA.md"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("overlay_dir")
    ap.add_argument("--commit", required=True)
    ap.add_argument("--strip", default="")
    ap.add_argument("--unclear", action="append", default=[])
    ap.add_argument("--note", action="append", default=[])
    args = ap.parse_args()

    root = pathlib.Path(args.overlay_dir)
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name not in SKIP):
        rel = path.relative_to(root).as_posix()
        upstream_rel = rel[len(args.strip):] if args.strip and rel.startswith(args.strip) else rel
        data = path.read_bytes()
        files.append({
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "upstream_path": f"PenguinTwitchBot/wwwroot/{upstream_rel}",
            "upstream_commit": args.commit,
            "license": LICENSE,
        })
    if not files:
        print("gen_assets: FAIL -- no files found (zero examined is a failure)", file=sys.stderr)
        return 1

    unclear = []
    for item in args.unclear:
        name, _, reason = item.partition("|")
        unclear.append({"path": name, "reason": reason, "vendored": False})

    manifest = {
        "upstream_repo": "https://github.com/Psychoboy/PenguinTwitchBot",
        "upstream_commit": args.commit,
        "license": LICENSE,
        "files": files,
        "third_party_or_unclear": unclear,
        "notes": args.note,
    }
    (root / "ASSETS.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"gen_assets: wrote {root / 'ASSETS.json'} ({len(files)} files, {len(unclear)} unclear)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
