#!/usr/bin/env python3
"""Plan and execute the changed-only alpha image build (deploy-dal2alpha).

Subcommands
  plan        Decide, per image, BUILD or REUSE the currently deployed image, resolve
         BuildKit lanes, and emit the build matrix + per-image Helm tag map.
  retag       For every REUSED image, copy the deployed manifest to the new alpha tag
              by digest (no rebuild) and verify digest equality.
  helm-values Write the Helm values file (global.imageTag + per-image global.imageTags).
  verify      Assert every long-lived Deployment runs exactly its planned image.

Sources of truth (no new secrets, no new permissions):
  * what is deployed  -> `helm get values` of the release (the runner already needs
    helm secrets access to `helm upgrade`); global.imageTags[<image>] else
    global.imageTag.
  * when it was built -> the org.opencontainers.image.revision label of that image
    in ghcr (per image, so an image that failed to build last time is compared to
    the commit it was actually built at, not to a global "last deploy").
Anything missing or unreadable fails safe to BUILD (see changed_images.py).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import changed_images as ci

REVISION_LABEL = "org.opencontainers.image.revision"


@dataclass(frozen=True)
class ImageInfo:
    """What the registry says about one image reference."""

    digest: str
    revision: str | None


def _config_labels(image_field: object) -> dict[str, str]:
    """Pull config.Labels out of an imagetools `.image` field (index or manifest)."""
    if not isinstance(image_field, dict):
        return {}
    cfg: dict[str, Any]
    if "config" in image_field:
        cfg = image_field
    else:
        picked = image_field.get("linux/amd64") or next(
            (v for v in image_field.values() if isinstance(v, dict) and "config" in v),
            {},
        )
        cfg = picked if isinstance(picked, dict) else {}
    labels = (cfg.get("config") or {}).get("Labels") or {}
    return dict(labels)


def registry_inspect(ref: str) -> ImageInfo | None:
    """Return digest + revision label for ``ref`` or None if it does not exist."""
    proc = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        digest = data["manifest"]["digest"]
    except (ValueError, KeyError):
        return None
    return ImageInfo(digest, _config_labels(data.get("image")).get(REVISION_LABEL))


def registry_retag(src_ref: str, digest: str, dst_ref: str) -> None:
    """Point ``dst_ref`` at the manifest ``src_ref@digest`` (registry-side copy)."""
    repo = src_ref.rsplit(":", 1)[0]
    subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            dst_ref,
            f"{repo}@{digest}",
        ],
        check=True,
    )


def probe_endpoint(url: str, timeout: float = 5.0) -> bool:
    """TCP-connect probe of a tcp://host:port BuildKit endpoint."""
    u = urlparse(url)
    if u.scheme != "tcp" or not u.hostname or not u.port:
        return False
    try:
        with socket.create_connection((u.hostname, u.port), timeout=timeout):
            return True
    except OSError:
        return False


def deployed_tags(
    values: dict[str, Any] | None, images: list[str]
) -> dict[str, str | None]:
    """Resolve each image's deployed tag from `helm get values -o json` output."""
    if not values:
        return {n: None for n in images}
    g = values.get("global") or {}
    per = g.get("imageTags") or {}
    default = g.get("imageTag") or None
    return {n: (per.get(n) or default) for n in images}


def resolve_lanes(
    imap: ci.ImageMap,
    urls: dict[str, str],
    probe: Callable[[str], bool] = probe_endpoint,
) -> tuple[dict[str, str], list[str]]:
    """Map lane -> effective endpoint URL.

    Lane `a` is mandatory. A lane with no URL, or whose endpoint fails the probe,
    folds into lane `a` with a warning (build capacity degrades, nothing breaks).
    """
    warnings: list[str] = []
    a = urls.get("a", "")
    if not a:
        raise SystemExit("ERROR: primary BuildKit endpoint (lane a) is not configured")
    effective = {"a": a}
    for lane in imap.lanes:
        if lane == "a":
            continue
        url = urls.get(lane, "")
        if not url:
            effective[lane] = a
        elif probe(url):
            effective[lane] = url
        else:
            effective[lane] = a
            warnings.append(
                f"BuildKit lane {lane} endpoint {url} unreachable; "
                f"falling back to lane a ({a})"
            )
    return effective, warnings


def build_plan(
    imap: ci.ImageMap,
    head: str,
    tag: str,
    registry: str,
    values: dict[str, Any] | None,
    force_all: bool,
    lane_urls: dict[str, str],
    diff_fn: ci.DiffFn,
    inspect: Callable[[str], ImageInfo | None] = registry_inspect,
    probe: Callable[[str], bool] = probe_endpoint,
) -> dict[str, Any]:
    """Compute the full plan dict (pure given the injected inspect/diff/probe)."""
    names = sorted(imap.images)
    tags = deployed_tags(values, names)
    bases: dict[str, str | None] = {}
    infos: dict[str, ImageInfo] = {}
    notes: dict[str, str] = {}
    for n in names:
        old = tags[n]
        if not old:
            notes[n] = "no deployed tag in Helm release values"
            bases[n] = None
            continue
        ref = f"{registry}/{n}:{old}{imap.images[n].tag_suffix}"
        info = inspect(ref)
        if info is None:
            notes[n] = f"deployed image {ref} not found in registry"
            bases[n] = None
        elif not info.revision:
            notes[n] = f"deployed image {ref} has no {REVISION_LABEL} label"
            bases[n] = None
        else:
            infos[n] = info
            bases[n] = info.revision
    decisions = ci.compute_plan(imap, bases, head, diff_fn, force_all)
    for n, why in notes.items():
        if decisions[n].build and not force_all:
            decisions[n].reasons.insert(0, why)
    build = sorted(n for n, d in decisions.items() if d.build)
    reuse = sorted(set(names) - set(build))
    lanes, warnings = resolve_lanes(imap, lane_urls, probe)
    matrix = ci.matrix_for(imap, build)
    for row in matrix:
        row["buildkit_host"] = lanes[row["lane"]]
    helm_tags = {n: (tag if n in build else tags[n]) for n in names}
    retag = [
        {
            "service": n,
            "suffix": imap.images[n].tag_suffix,
            "old_tag": tags[n],
            "digest": infos[n].digest,
            "src": f"{registry}/{n}:{tags[n]}{imap.images[n].tag_suffix}",
            "dst": f"{registry}/{n}:{tag}{imap.images[n].tag_suffix}",
        }
        for n in reuse
    ]
    return {
        "head": head,
        "tag": tag,
        "force_all": force_all,
        "examined": len(names),
        "build": build,
        "reuse": reuse,
        "decisions": {n: d.as_dict() for n, d in decisions.items()},
        "matrix": {"include": matrix},
        "helm_tags": helm_tags,
        "retag": retag,
        "lanes": lanes,
        "warnings": warnings,
    }


def _emit_outputs(plan: dict[str, Any]) -> None:
    """Append step outputs to $GITHUB_OUTPUT when running in Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write(f"matrix={json.dumps(plan['matrix'], separators=(',', ':'))}\n")
        fh.write(f"build_count={len(plan['build'])}\n")
        fh.write(f"retag_count={len(plan['retag'])}\n")
        fh.write(f"plan={json.dumps(plan, separators=(',', ':'))}\n")


def cmd_plan(args: argparse.Namespace) -> int:
    """`plan` subcommand."""
    imap = ci.load_map(args.map)
    head = subprocess.run(
        ["git", "rev-parse", args.head], check=True, capture_output=True, text=True
    ).stdout.strip()
    values = None
    if args.helm_values and Path(args.helm_values).is_file():
        try:
            values = json.loads(Path(args.helm_values).read_text() or "null")
        except ValueError as exc:
            print(f"::warning::helm values unreadable ({exc}); building all images")
    else:
        print(
            "::warning::no Helm release values (first deploy or unreadable); "
            "building all images"
        )
    lane_urls = {"a": args.endpoint_a or "", "b": args.endpoint_b or ""}
    force = args.force_all or os.environ.get("FORCE_ALL", "").lower() == "true"
    plan = build_plan(
        imap, head, args.tag, args.registry, values, force, lane_urls, ci.git_diff_names
    )
    if plan["examined"] == 0:
        print("ERROR: zero images examined", file=sys.stderr)
        return 1
    for w in plan["warnings"]:
        print(f"::warning::{w}")
    for n in sorted(plan["decisions"]):
        d = plan["decisions"][n]
        verdict = "BUILD" if d["build"] else "REUSE"
        why = "; ".join(d["reasons"]) or "no input changed"
        print(f"{verdict:<5} {n:<22} base={str(d['base'])[:12]:<12} {why}")
    print(
        f"examined={plan['examined']} build={len(plan['build'])} "
        f"reuse={len(plan['reuse'])} force_all={plan['force_all']}"
    )
    Path(args.out).write_text(json.dumps(plan, indent=2))
    _emit_outputs(plan)
    return 0


def cmd_retag(args: argparse.Namespace) -> int:
    """`retag` subcommand: copy reused manifests to the new tag, verify digests."""
    plan = json.loads(Path(args.plan).read_text())
    rows = plan["retag"]
    if not rows:
        print(
            "ERROR: retag invoked with zero images (job should have been skipped)",
            file=sys.stderr,
        )
        return 1
    bad = 0
    for r in rows:
        try:
            registry_retag(r["src"], r["digest"], r["dst"])
        except subprocess.CalledProcessError as exc:
            print(f"::error::retag {r['service']}: {exc}")
            bad += 1
            continue
        after = registry_inspect(r["dst"])
        if after is None or after.digest != r["digest"]:
            got = after.digest if after else "missing"
            print(
                f"::error::retag {r['service']}: digest mismatch want={r['digest']} got={got}"
            )
            bad += 1
        else:
            print(
                f"retagged {r['service']}: {r['src']} -> {r['dst']} ({r['digest'][:19]})"
            )
    print(f"examined={len(rows)} ok={len(rows) - bad} failed={bad}")
    return 1 if bad else 0


def cmd_helm_values(args: argparse.Namespace) -> int:
    """`helm-values` subcommand: JSON (valid YAML) values file for helm -f."""
    plan = json.loads(Path(args.plan).read_text())
    if not plan["helm_tags"]:
        print("ERROR: plan has no image tags", file=sys.stderr)
        return 1
    doc = {"global": {"imageTag": plan["tag"], "imageTags": plan["helm_tags"]}}
    Path(args.out).write_text(json.dumps(doc, indent=2, sort_keys=True))
    print(
        f"wrote {args.out}: {len(plan['helm_tags'])} per-image tags "
        f"(global.imageTag={plan['tag']})"
    )
    return 0


def expected_deployment_images(
    plan: dict[str, Any], registry: str, imap: ci.ImageMap
) -> dict[str, str]:
    """image name -> exact image ref the long-lived Deployments must run."""
    return {
        n: f"{registry}/{n}:{plan['helm_tags'][n]}{imap.images[n].tag_suffix}"
        for n in sorted(imap.images)
        if imap.images[n].workload == "deployment"
    }


def cmd_verify(args: argparse.Namespace) -> int:
    """`verify` subcommand: stdin = whitespace-separated live container images."""
    imap = ci.load_map(args.map)
    plan = json.loads(Path(args.plan).read_text())
    live = set(sys.stdin.read().split())
    want = expected_deployment_images(plan, args.registry, imap)
    missing = {n: ref for n, ref in want.items() if ref not in live}
    for n, ref in want.items():
        print(f"{'MISSING' if n in missing else 'ok     '} {n:<22} {ref}")
    print(f"examined={len(want)} ok={len(want) - len(missing)} missing={len(missing)}")
    if not want:
        print("ERROR: zero images examined", file=sys.stderr)
        return 1
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--map", default=str(ci.DEFAULT_MAP))
    p.add_argument("--head", default="HEAD")
    p.add_argument("--tag", required=True, help="new alpha-<epoch> tag")
    p.add_argument(
        "--registry", required=True, help="e.g. ghcr.io/penguintechinc/waddles"
    )
    p.add_argument("--helm-values", help="JSON from `helm get values <rel> -o json`")
    p.add_argument("--force-all", action="store_true")
    p.add_argument("--endpoint-a", default=os.environ.get("BUILDKIT_HOST", ""))
    p.add_argument("--endpoint-b", default=os.environ.get("BUILDKIT_HOST_B", ""))
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_plan)
    r = sub.add_parser("retag")
    r.add_argument("--plan", required=True)
    r.set_defaults(fn=cmd_retag)
    h = sub.add_parser("helm-values")
    h.add_argument("--plan", required=True)
    h.add_argument("--out", required=True)
    h.set_defaults(fn=cmd_helm_values)
    v = sub.add_parser("verify")
    v.add_argument("--map", default=str(ci.DEFAULT_MAP))
    v.add_argument("--plan", required=True)
    v.add_argument("--registry", required=True)
    v.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    rc: int = args.fn(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
