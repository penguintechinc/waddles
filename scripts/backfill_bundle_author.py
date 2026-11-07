#!/usr/bin/env python3
"""Backfill the validated `author` (and `collection`) on every first-party bundle manifest.

Every bundle under bundles/{python,rust,csharp,javascript}/* gets `author: github:penguintechinc`
(csharp/superpenguin-roll gets `author: github:Psychoboy` + `collection: superpenguintv`). A
manifest change needs a patch version bump, applied in lockstep to bundle.yaml,
hub-manifest.yaml, pyproject.toml (python) and the bundles/core-bundles.yaml catalog row.

Idempotent: a bundle whose manifests already carry the target author (and collection) is
skipped, so re-running never bumps twice. Usage: backfill_bundle_author.py [--dry-run]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLES = ROOT / "bundles"
DEFAULT_AUTHOR = "github:penguintechinc"
OVERRIDES: dict[str, tuple[str, str | None]] = {
    "csharp/superpenguin-roll": ("github:Psychoboy", "superpenguintv"),
}
_AUTHOR_RE = re.compile(r"^author:.*\n", re.MULTILINE)
_VERSION_RE = re.compile(
    r"^version:\s*[\"']?([0-9][^\s\"']*)[\"']?[ \t]*$", re.MULTILINE
)
_COLLECTION_RE = re.compile(r"^collection:.*\n", re.MULTILINE)


def bump(version: str) -> str:
    """Patch-bump a MAJOR.MINOR.PATCH string."""
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def set_fields(text: str, author: str, collection: str | None, new_version: str) -> str:
    """Return `text` with author (+collection) set and the top-level version replaced."""
    line = f"author: {author}\n"
    if _AUTHOR_RE.search(text):
        text = _AUTHOR_RE.sub(line, text, count=1)
    else:
        provider = re.search(r"^provider:.*\n", text, re.MULTILINE)
        if provider is None:
            raise SystemExit("manifest has no provider line to anchor author")
        text = text[: provider.end()] + line + text[provider.end() :]
    if collection:
        cline = f"collection: {collection}\n"
        if _COLLECTION_RE.search(text):
            text = _COLLECTION_RE.sub(cline, text, count=1)
        else:
            a = _AUTHOR_RE.search(text)
            assert a is not None
            text = text[: a.end()] + cline + text[a.end() :]
    return _VERSION_RE.sub(f"version: {new_version}", text, count=1)


def main() -> int:
    """Process every bundle directory; print counts."""
    dry = "--dry-run" in sys.argv
    catalog_path = BUNDLES / "core-bundles.yaml"
    catalog = catalog_path.read_text()
    changed = skipped = no_row = 0
    for bdir in sorted(
        p
        for lang in ("python", "rust", "csharp", "javascript")
        for p in (BUNDLES / lang).iterdir()
        if p.is_dir()
    ):
        rel = f"{bdir.parent.name}/{bdir.name}"
        author, collection = OVERRIDES.get(rel, (DEFAULT_AUTHOR, None))
        files = [
            bdir / n
            for n in ("hub-manifest.yaml", "bundle.yaml")
            if (bdir / n).exists()
        ]
        if not files:
            continue
        texts = {f: f.read_text() for f in files}
        done = all(
            re.search(rf"^author: {re.escape(author)}$", t, re.MULTILINE)
            and (
                not collection
                or re.search(rf"^collection: {collection}$", t, re.MULTILINE)
            )
            for t in texts.values()
        )
        if done:
            skipped += 1
            continue
        versions = {m.group(1) for t in texts.values() if (m := _VERSION_RE.search(t))}
        if len(versions) != 1:
            raise SystemExit(f"{rel}: manifests disagree on version {sorted(versions)}")
        old = versions.pop()
        new = bump(old)
        for f, t in texts.items():
            t = set_fields(t, author, collection, new)
            t = t.replace(f'tested_with: "{old}"', f'tested_with: "{new}"')
            t = t.replace(f'min_version: "{old}"', f'min_version: "{new}"')
            if not dry:
                f.write_text(t)
        pyproject = bdir / "pyproject.toml"
        if pyproject.exists() and not dry:
            pyproject.write_text(
                re.sub(
                    r'^version = "[^"]+"',
                    f'version = "{new}"',
                    pyproject.read_text(),
                    count=1,
                    flags=re.MULTILINE,
                )
            )
        app_id = re.search(r"^app_id:\s*(\S+)", texts[files[0]], re.MULTILINE)
        assert app_id is not None
        pat = re.compile(
            rf'(- app_id: {re.escape(app_id.group(1))}\n\s+version: ")[^"]+(")'
        )
        catalog, n = pat.subn(rf"\g<1>{new}\g<2>", catalog)
        no_row += 0 if n else 1
        if not n:
            print(f"NO-CATALOG-ROW {rel} ({app_id.group(1)})")
        changed += 1
    if not dry:
        catalog_path.write_text(catalog)
    print(
        f"bundles changed={changed} already-done={skipped} without-catalog-row={no_row}"
        f"{' (dry-run)' if dry else ''}"
    )
    return 0 if changed + skipped > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
