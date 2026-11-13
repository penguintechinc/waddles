#!/usr/bin/env python3
"""Render-level proof that per-image Helm pins restart only the workloads that changed.

Renders the chart three ways with the real values-alpha + values-dal2alpha overlays and compares
Deployment/StatefulSet/DaemonSet/CronJob/Job pod templates (Secrets are excluded: the chart
generates random credentials per render, which is noise here):

  1. unpinned global.imageTag=T1 vs unpinned T1             -> 0 changed (determinism)
  2. T1 baseline vs global.imageTag=T2 with EVERY image pinned to T1 -> 0 changed
  3. T1 baseline vs T2 with only svc-ingest + hub-api pinned to T2   -> exactly the svc-ingest
     Deployment, the hub-api Deployment and the hub-api CronJobs change

Exit 1 on any mismatch or if helm renders nothing (zero examined is a failure). Needs helm v3 on
PATH (CI/local: ~/.local/bin/helm3 first on PATH).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "k8s/helm/waddlebot"
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job"}
IMAGES = [
    "hub-api",
    "hub-webui",
    "migrations",
    "seeder",
    "svc-ingest",
    "svc-process",
    "svc-action",
    "svc-presentation",
    "svc-bundles-executor",
    "svc-streaming",
    "egress-proxy",
]


def render(
    tag: str, pins: dict[str, str] | None, workdir: Path
) -> dict[tuple[str, str], Any]:
    """Render the chart and return {(kind, name): pod template} for workloads."""
    cmd = [
        "helm",
        "template",
        "waddlebot",
        str(CHART),
        "-n",
        "waddlebot",
        "-f",
        str(CHART / "values-alpha.yaml"),
        "-f",
        str(CHART / "values-dal2alpha.yaml"),
        "--set-string",
        f"global.imageTag={tag}",
    ]
    if pins is not None:
        f = (
            workdir
            / f"pins-{tag}-{len(pins)}-{abs(hash(tuple(sorted(pins.items()))))}.json"
        )
        f.write_text(json.dumps({"global": {"imageTags": pins}}))
        cmd += ["-f", str(f)]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    res: dict[tuple[str, str], Any] = {}
    for d in yaml.safe_load_all(out):
        if not d or d.get("kind") not in WORKLOADS:
            continue
        spec = d["spec"]
        tpl = (
            spec["jobTemplate"]["spec"]["template"]
            if d["kind"] == "CronJob"
            else spec["template"]
        )
        res[(d["kind"], d["metadata"]["name"])] = tpl
    return res


def changed(a: dict[tuple[str, str], Any], b: dict[tuple[str, str], Any]) -> list[str]:
    """Names of workloads whose pod template differs."""
    return sorted(f"{k[0]}/{k[1]}" for k in set(a) | set(b) if a.get(k) != b.get(k))


def main() -> int:
    """Run the three render comparisons."""
    t1, t2 = "alpha-1111111111", "alpha-2222222222"
    failures = 0
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        base = render(t1, None, wd)
        if not base:
            print("FAIL: helm rendered zero workloads", file=sys.stderr)
            return 1
        print(f"workloads examined: {len(base)}")

        got = changed(base, render(t1, None, wd))
        print(f"1. determinism (same tag, unpinned): changed={len(got)}")
        failures += bool(got)

        got = changed(base, render(t2, dict.fromkeys(IMAGES, t1), wd))
        print(f"2. global={t2}, all images pinned to {t1}: changed={len(got)}")
        failures += bool(got)

        pins = dict.fromkeys(IMAGES, t1) | {"svc-ingest": t2, "hub-api": t2}
        new = render(t2, pins, wd)
        got = changed(base, new)
        print(f"3. svc-ingest + hub-api rebuilt: changed={len(got)}")
        for n in got:
            print(f"   {n}")
        bad = []
        for kind_name in got:
            k = tuple(kind_name.split("/", 1))
            repos = {
                c["image"].rsplit(":", 1)[0].rsplit("/", 1)[-1]
                for c in new[k]["spec"]["containers"]  # type: ignore[index]
                if c["image"].endswith(t2)
            }
            if not repos or not repos <= {"svc-ingest", "hub-api"}:
                bad.append(kind_name)
        if (
            bad
            or not any("svc-ingest" in n for n in got)
            or not any("hub-api" in n for n in got)
        ):
            print(f"FAIL: unexpected restart set: {bad or got}", file=sys.stderr)
            failures += 1
    print("PASS" if not failures else f"FAIL: {failures} comparison(s) wrong")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
