#!/usr/bin/env python3
"""Prune old alpha image tags and stale BuildKit cache refs in ghcr.io.

DRY-RUN BY DEFAULT. Nothing is deleted unless --apply is passed, and --apply additionally
requires --deployed (the set of tags that must survive). This script is NOT wired to any
schedule or workflow; enabling one needs the owner's approval.

Per image package ``<prefix>/<image>`` (names from scripts/ci/image-inputs.yaml):
  * keep the newest N distinct alpha-<epoch> epochs (default 10) -- variants such as
    ``-cpu`` share an epoch -- plus every tag listed as deployed;
  * a package version is deleted only if ALL of its tags are prunable alpha tags;
  * versions with no tag are NEVER touched (they are the child manifests / attestations that
    tagged index versions reference; deleting them breaks pulls).
Per cache package ``<prefix>/buildcache/<image>``:
  * the single ref ``alpha`` is kept; any other tag is a stale per-run cache ref;
  * a cache package for an image that is not in the map is stale as a whole.

Auth: token from $GH_TOKEN or $GITHUB_TOKEN (never printed). Needs read:packages for the
dry run and delete:packages for --apply.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
import changed_images as ci

ALPHA_TAG = re.compile(r"^alpha-(\d{10,})(?:-[a-z0-9]+)?$")
CACHE_KEEP_TAG = "alpha"
API = "https://api.github.com"


class GhApi:
    """Minimal GitHub REST client (token never logged)."""

    def __init__(self, token: str) -> None:
        self._token = token

    def _req(self, method: str, url: str) -> Any:
        req = urllib.request.Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else None

    def get_pages(self, path: str) -> list[dict[str, Any]]:
        """GET every page of a list endpoint."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            chunk = self._req("GET", f"{API}{path}{sep}per_page=100&page={page}")
            if not chunk:
                return out
            out.extend(chunk)
            if len(chunk) < 100:
                return out
            page += 1

    def delete(self, path: str) -> None:
        """DELETE one resource."""
        self._req("DELETE", f"{API}{path}")


class ApiLike(Protocol):
    """The two calls `run` needs (GhApi in production, a fake in tests)."""

    def get_pages(self, path: str) -> list[dict[str, Any]]:
        """List every item of an endpoint."""

    def delete(self, path: str) -> None:
        """Delete one resource."""


@dataclass
class Result:
    """Counts and per-item verdicts for one run."""

    examined: int = 0
    keep: list[str] = field(default_factory=list)
    delete: list[tuple[str, int, list[str]]] = field(
        default_factory=list
    )  # (package, id, tags)
    skipped_untagged: int = 0
    stale_packages: list[str] = field(default_factory=list)


def _pkg_path(org: str, package: str) -> str:
    return f"/orgs/{org}/packages/container/{urllib.parse.quote(package, safe='')}"


def _tags(version: dict[str, Any]) -> list[str]:
    return ((version.get("metadata") or {}).get("container") or {}).get("tags") or []


def plan_image_package(
    package: str, versions: list[dict[str, Any]], keep: int, deployed: set[str]
) -> Result:
    """Decide keep/delete for one image package (pure)."""
    res = Result()
    epochs: dict[int, None] = {}
    for v in versions:
        for t in _tags(v):
            m = ALPHA_TAG.match(t)
            if m:
                epochs[int(m.group(1))] = None
    keep_epochs = set(sorted(epochs, reverse=True)[:keep])
    for v in versions:
        res.examined += 1
        tags = _tags(v)
        if not tags:
            res.skipped_untagged += 1
            continue
        protected = False
        prunable = True
        for t in tags:
            m = ALPHA_TAG.match(t)
            if not m:
                prunable = False  # a non-alpha tag (e.g. latest, beta-*) is never ours to prune
            elif t in deployed or int(m.group(1)) in keep_epochs:
                protected = True
        if prunable and not protected:
            res.delete.append((package, v["id"], tags))
        else:
            res.keep.append(f"{package}: {','.join(tags)}")
    return res


def plan_cache_package(
    package: str, image: str, known: set[str], versions: list[dict[str, Any]]
) -> Result:
    """Decide keep/delete for one buildcache package (pure)."""
    res = Result()
    stale_image = image not in known
    if stale_image:
        res.stale_packages.append(package)
    for v in versions:
        res.examined += 1
        tags = _tags(v)
        if not tags:
            res.skipped_untagged += 1
            continue
        if stale_image or CACHE_KEEP_TAG not in tags:
            res.delete.append((package, v["id"], tags))
        else:
            res.keep.append(f"{package}: {','.join(tags)}")
    return res


def deployed_tags_from(path: str | None) -> set[str] | None:
    """Tags that must survive, from a plan.json (helm_tags) or helm values JSON; None if unset."""
    if not path:
        return None
    doc = json.loads(Path(path).read_text())
    tags: set[str] = set()
    if "helm_tags" in doc:
        tags.update(doc["helm_tags"].values())
    g = doc.get("global") or {}
    if g.get("imageTag"):
        tags.add(g["imageTag"])
    tags.update((g.get("imageTags") or {}).values())
    return tags


def expand_variants(tags: set[str], imap: ci.ImageMap) -> set[str]:
    """Add suffixed variants (alpha-<epoch>-cpu) of every deployed base tag."""
    out = set(tags)
    for t in tags:
        for img in imap.images.values():
            if img.tag_suffix:
                out.add(t + img.tag_suffix)
    return out


def run(
    api: ApiLike,
    org: str,
    prefix: str,
    imap: ci.ImageMap,
    keep: int,
    deployed: set[str],
    apply: bool,
    out: Callable[[str], None] = print,
) -> int:
    """Walk every image + cache package; print the plan; delete only if ``apply``."""
    total = Result()
    for name in sorted(imap.images):
        pkg = f"{prefix}/{name}"
        try:
            versions = api.get_pages(_pkg_path(org, pkg) + "/versions")
        except urllib.error.HTTPError as exc:
            out(f"WARN {pkg}: {exc.code} {exc.reason}; skipped")
            continue
        r = plan_image_package(pkg, versions, keep, deployed)
        total.examined += r.examined
        total.keep += r.keep
        total.delete += r.delete
        total.skipped_untagged += r.skipped_untagged
    try:
        pkgs = api.get_pages(f"/orgs/{org}/packages?package_type=container")
    except urllib.error.HTTPError as exc:
        out(f"WARN could not list org packages ({exc.code}); cache pruning skipped")
        pkgs = []
    cache_prefix = f"{prefix}/buildcache/"
    for p in pkgs:
        pname = p.get("name", "")
        if not pname.startswith(cache_prefix):
            continue
        image = pname[len(cache_prefix) :]
        versions = api.get_pages(_pkg_path(org, pname) + "/versions")
        r = plan_cache_package(pname, image, set(imap.images), versions)
        total.examined += r.examined
        total.keep += r.keep
        total.delete += r.delete
        total.stale_packages += r.stale_packages
        total.skipped_untagged += r.skipped_untagged
    for pkg, vid, tags in total.delete:
        out(
            f"{'DELETE' if apply else 'would delete'} {pkg} version={vid} tags={','.join(tags)}"
        )
    for s in total.stale_packages:
        out(f"stale cache package (image not in map): {s}")
    out(
        f"examined={total.examined} keep={len(total.keep)} delete={len(total.delete)} "
        f"untagged_skipped={total.skipped_untagged} stale_cache_packages={len(total.stale_packages)} "
        f"mode={'APPLY' if apply else 'DRY-RUN'}"
    )
    if total.examined == 0:
        out("ERROR: zero package versions examined (wrong org/prefix or no access?)")
        return 1
    if apply:
        for pkg, vid, _ in total.delete:
            api.delete(_pkg_path(org, pkg) + f"/versions/{vid}")
        out(f"deleted={len(total.delete)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--org", default="penguintechinc")
    ap.add_argument("--prefix", default="waddles", help="package name prefix (repo)")
    ap.add_argument(
        "--keep", type=int, default=10, help="newest alpha epochs to keep per image"
    )
    ap.add_argument("--map", default=str(ci.DEFAULT_MAP))
    ap.add_argument(
        "--deployed",
        help="plan.json or `helm get values -o json` file: tags to protect",
    )
    ap.add_argument(
        "--apply", action="store_true", help="actually delete (default: dry run)"
    )
    args = ap.parse_args(argv)
    if args.keep < 1:
        print("ERROR: --keep must be >= 1", file=sys.stderr)
        return 2
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: set GH_TOKEN or GITHUB_TOKEN", file=sys.stderr)
        return 2
    imap = ci.load_map(args.map)
    deployed = deployed_tags_from(args.deployed)
    if args.apply and deployed is None:
        print(
            "ERROR: --apply requires --deployed (never prune without knowing what is live)",
            file=sys.stderr,
        )
        return 2
    if deployed is None:
        print(
            "WARN: no --deployed file; the currently deployed tag is only protected by the "
            f"newest-{args.keep} rule (fine for a dry run, required for --apply)"
        )
    return run(
        GhApi(token),
        args.org,
        args.prefix,
        imap,
        args.keep,
        expand_variants(deployed or set(), imap),
        args.apply,
    )


if __name__ == "__main__":
    sys.exit(main())
