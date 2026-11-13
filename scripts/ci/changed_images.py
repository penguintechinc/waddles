#!/usr/bin/env python3
"""Map a git diff to the set of alpha images that must be rebuilt.

The input map lives in ``scripts/ci/image-inputs.yaml``. Every image lists the
path globs its Dockerfile consumes; ``shared_all`` globs invalidate every image
and ``ignore`` globs are paths that provably affect no image. A changed path
that matches none of these is UNMAPPED and fails safe to "rebuild everything",
so the map can never silently rot.

Fail-safe rules (each forces a rebuild of the affected image, with a reason):
  * ``--force-all`` / force_all=true
  * the base sha for the image is unknown, missing locally, or the diff fails
  * the diff is empty although base != head (suspicious)
  * the diff touches an UNMAPPED path (rebuilds every image)
  * the diff touches a ``shared_all`` path (rebuilds every image)

Library use: ``compute_plan``. CLI: ``changed_images.py --help``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import yaml  # type: ignore[import-untyped]

DEFAULT_MAP = Path(__file__).with_name("image-inputs.yaml")
ZERO_SHA = "0" * 40


class MapError(Exception):
    """The image-input map is malformed."""


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a repo-root-anchored glob to a regex.

    ``*`` stays within one path segment, ``**`` crosses segments, ``dir/`` and
    ``dir/**`` match everything below ``dir``.
    """
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == n:
            out.append("/.*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


@dataclass(frozen=True)
class Image:
    """One deployable image and everything needed to build and gate it."""

    name: str
    dockerfile: str
    context: str
    build_contexts: tuple[str, ...]
    target: str
    tag_suffix: str
    lane: str
    workload: str
    inputs: tuple[str, ...]


@dataclass
class ImageMap:
    """Parsed ``image-inputs.yaml``."""

    images: dict[str, Image]
    shared_all: tuple[str, ...]
    ignore: tuple[str, ...]
    lanes: dict[str, str]
    _input_re: dict[str, list[re.Pattern[str]]] = field(default_factory=dict)
    _shared_re: list[re.Pattern[str]] = field(default_factory=list)
    _ignore_re: list[re.Pattern[str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._input_re = {
            n: [glob_to_regex(g) for g in img.inputs] for n, img in self.images.items()
        }
        self._shared_re = [glob_to_regex(g) for g in self.shared_all]
        self._ignore_re = [glob_to_regex(g) for g in self.ignore]

    def classify(self, path: str) -> tuple[str, frozenset[str]]:
        """Return (kind, images) for one repo-relative path.

        kind is "shared" (all images), "image", "ignored" or "unmapped".
        """
        if any(r.match(path) for r in self._shared_re):
            return "shared", frozenset(self.images)
        hit = frozenset(
            n for n, rs in self._input_re.items() if any(r.match(path) for r in rs)
        )
        if hit:
            return "image", hit
        if any(r.match(path) for r in self._ignore_re):
            return "ignored", frozenset()
        return "unmapped", frozenset()


def load_map(path: Path | str = DEFAULT_MAP) -> ImageMap:
    """Load and validate the YAML map; raise MapError on any structural problem."""
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise MapError("image-inputs.yaml: expected a mapping with version: 1")
    lanes = raw.get("lanes") or {}
    if not lanes:
        raise MapError("image-inputs.yaml: no lanes defined")
    groups = raw.get("groups") or {}
    images: dict[str, Image] = {}
    for name, spec in (raw.get("images") or {}).items():
        inputs: list[str] = list(spec.get("inputs") or [])
        for g in spec.get("groups") or []:
            if g not in groups:
                raise MapError(f"image {name}: unknown group {g!r}")
            inputs.extend(groups[g])
        if not inputs:
            raise MapError(f"image {name}: no inputs")
        lane = spec.get("lane", "a")
        if lane not in lanes:
            raise MapError(f"image {name}: unknown lane {lane!r}")
        if spec.get("workload", "deployment") not in ("deployment", "job"):
            raise MapError(f"image {name}: workload must be deployment|job")
        for key in ("dockerfile", "context"):
            if key not in spec:
                raise MapError(f"image {name}: missing {key}")
        dockerfile = spec["dockerfile"]
        if dockerfile not in inputs:
            inputs.append(dockerfile)
        images[name] = Image(
            name=name,
            dockerfile=dockerfile,
            context=spec["context"],
            build_contexts=tuple(spec.get("build_contexts") or ()),
            target=spec.get("target", ""),
            tag_suffix=spec.get("tag_suffix", ""),
            lane=lane,
            workload=spec.get("workload", "deployment"),
            inputs=tuple(inputs),
        )
    if not images:
        raise MapError("image-inputs.yaml: no images defined")
    return ImageMap(
        images=images,
        shared_all=tuple(raw.get("shared_all") or ()),
        ignore=tuple(raw.get("ignore") or ()),
        lanes=lanes,
    )


@dataclass
class Decision:
    """Build/skip verdict for one image, with every reason that applied."""

    image: str
    build: bool
    reasons: list[str]
    base: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable view."""
        return {"build": self.build, "reasons": self.reasons, "base": self.base}


DiffFn = Callable[[str, str], list[str]]


def git_diff_names(base: str, head: str, repo: str | None = None) -> list[str]:
    """Tree-to-tree changed paths between two commits; raise on any git failure."""
    cmd = (
        ["git"]
        + (["-C", repo] if repo else [])
        + [
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            base,
            head,
        ]
    )
    out = subprocess.run(cmd, check=True, capture_output=True).stdout.decode()
    return [p for p in out.split("\0") if p]


def compute_plan(
    imap: ImageMap,
    bases: Mapping[str, str | None],
    head: str,
    diff_fn: DiffFn,
    force_all: bool = False,
) -> dict[str, Decision]:
    """Decide build/skip per image.

    ``bases`` maps image -> last-built revision sha (None if unknown). ``diff_fn``
    returns changed paths between two shas. Any uncertainty yields build=True.
    """
    decisions = {n: Decision(n, False, [], bases.get(n)) for n in imap.images}
    if force_all:
        for d in decisions.values():
            d.build, d.reasons = True, ["force_all"]
        return decisions

    diff_cache: dict[str, list[str] | str] = {}

    def paths_for(base: str) -> list[str] | str:
        """Changed paths for ``base``..head, or an error string (memoised)."""
        if base not in diff_cache:
            try:
                diff_cache[base] = diff_fn(base, head)
            except Exception as exc:  # noqa: BLE001 - fail safe: any diff problem rebuilds
                diff_cache[base] = f"diff {base[:12]}..{head[:12]} failed: {exc}"
        return diff_cache[base]

    build_everything: list[str] = []
    for name, d in decisions.items():
        base = bases.get(name)
        if not base or not re.fullmatch(r"[0-9a-f]{40}", base) or base == ZERO_SHA:
            d.build, d.reasons = True, ["no known last-built sha for this image"]
            continue
        paths = paths_for(base)
        if isinstance(paths, str):
            d.build, d.reasons = True, [paths]
            continue
        if base == head:
            continue
        if not paths:
            d.build, d.reasons = (
                True,
                [
                    f"empty diff although base {base[:12]} != head {head[:12]} (suspicious)"
                ],
            )
            continue
        for p in paths:
            kind, hit = imap.classify(p)
            if kind == "unmapped":
                build_everything.append(f"unmapped path {p!r}")
            elif kind == "shared":
                build_everything.append(f"shared input {p!r}")
            elif name in hit:
                d.build = True
                if len(d.reasons) < 3:
                    d.reasons.append(f"input changed: {p}")
    if build_everything:
        for d in decisions.values():
            d.build = True
            d.reasons.insert(0, f"build-all: {build_everything[0]}")
    return decisions


def audit(imap: ImageMap, files: Iterable[str]) -> dict[str, Any]:
    """Classify every file; report unmapped ones and the examined count."""
    counts = {"shared": 0, "image": 0, "ignored": 0, "unmapped": 0}
    unmapped: list[str] = []
    examined = 0
    for p in files:
        examined += 1
        kind, _ = imap.classify(p)
        counts[kind] += 1
        if kind == "unmapped":
            unmapped.append(p)
    return {"examined": examined, "counts": counts, "unmapped": unmapped}


def tracked_files(repo: str | None = None) -> list[str]:
    """All tracked paths at HEAD."""
    cmd = ["git"] + (["-C", repo] if repo else []) + ["ls-files", "-z"]
    out = subprocess.run(cmd, check=True, capture_output=True).stdout.decode()
    return [p for p in out.split("\0") if p]


def matrix_for(imap: ImageMap, build: list[str]) -> list[dict[str, str]]:
    """Build-matrix rows (workflow fromJSON) for the images to build."""
    return [
        {
            "service": n,
            "file": imap.images[n].dockerfile,
            "context": imap.images[n].context,
            "extra_ctx": "\n".join(imap.images[n].build_contexts),
            "target": imap.images[n].target,
            "suffix": imap.images[n].tag_suffix,
            "lane": imap.images[n].lane,
        }
        for n in build
    ]


def _print_table(decisions: dict[str, Decision], out: TextIO = sys.stderr) -> None:
    """Human-readable verdict table."""
    for n, d in sorted(decisions.items()):
        verdict = "BUILD" if d.build else "skip "
        why = "; ".join(d.reasons) if d.reasons else "no input changed"
        print(f"  {verdict} {n:<22} {why}", file=out)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--map", default=str(DEFAULT_MAP))
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--base", help="one base sha applied to every image")
    ap.add_argument("--bases-json", help="JSON file: {image: sha|null}")
    ap.add_argument("--force-all", action="store_true")
    ap.add_argument("--repo", help="git repo dir (default: cwd)")
    ap.add_argument(
        "--audit",
        action="store_true",
        help="classify every tracked file; exit 1 if any is unmapped",
    )
    ap.add_argument("--format", choices=("json", "names", "matrix"), default="json")
    args = ap.parse_args(argv)

    try:
        imap = load_map(args.map)
    except (MapError, OSError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.audit:
        files = tracked_files(args.repo)
        res = audit(imap, files)
        print(json.dumps(res, indent=2))
        if res["examined"] == 0 or res["unmapped"]:
            print(
                f"FAIL: examined={res['examined']} unmapped={len(res['unmapped'])}",
                file=sys.stderr,
            )
            return 1
        print(f"OK: examined={res['examined']} unmapped=0", file=sys.stderr)
        return 0

    head = subprocess.run(
        ["git"] + (["-C", args.repo] if args.repo else []) + ["rev-parse", args.head],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    bases: dict[str, str | None] = {n: None for n in imap.images}
    if args.bases_json:
        loaded = json.loads(Path(args.bases_json).read_text())
        bases.update({k: v for k, v in loaded.items() if k in bases})
    if args.base:
        base = subprocess.run(
            ["git"]
            + (["-C", args.repo] if args.repo else [])
            + ["rev-parse", args.base],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        bases = {n: base for n in imap.images}
    force = args.force_all or os.environ.get("FORCE_ALL", "").lower() == "true"
    decisions = compute_plan(
        imap, bases, head, lambda b, h: git_diff_names(b, h, args.repo), force
    )
    build = sorted(n for n, d in decisions.items() if d.build)
    _print_table(decisions)
    examined = len(decisions)
    print(
        f"examined={examined} build={len(build)} skip={examined - len(build)}",
        file=sys.stderr,
    )
    if examined == 0:
        return 1
    if args.format == "names":
        print("\n".join(build))
    elif args.format == "matrix":
        print(json.dumps({"include": matrix_for(imap, build)}))
    else:
        print(
            json.dumps(
                {
                    "head": head,
                    "build": build,
                    "skip": sorted(set(decisions) - set(build)),
                    "decisions": {n: d.as_dict() for n, d in decisions.items()},
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
