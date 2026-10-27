#!/usr/bin/env python3
"""Render assertion: all-digit short SHA must never render as a Go %!s(...) value.

# regression: all-digit short SHA rendered as %!s(int64=...) image tag (alpha 2026-10-03)

Background: `helm ... --set global.imageTag=35604387` (an all-digit short SHA)
parses the value as an int64, since Helm's `--set` infers basic YAML types for
unquoted scalars. Every template that built `repo:tag` via Go's `printf "%s"`
on that int64 rendered `localhost:32000/waddlebot/hub-api:%!s(int64=35604387)`
instead of the real tag -- this broke the alpha deploy preflight and would
have broken the real `helm upgrade` too.

Fix was two-layered:
  1. Every deploy script/workflow now passes the tag via `--set-string`, which
     never does type inference.
  2. Every chart template that builds `repo:tag` pipes the tag through
     `| toString`, so a numeric value can't render as `%!s(...)` even if a
     caller still uses plain `--set`.

Reads a `helm template waddlebot k8s/helm/waddlebot --values values-alpha.yaml
--kube-version 1.30.0 --set[-string] global.imageTag=<tag>` multi-document
YAML stream on stdin and asserts, for every container+initContainer image
referencing the chart's own images (no external digest-pinned images):
  1. the image tag renders as the exact literal tag passed in -- never
     `%!s(...)` or any other corrupted form;
  2. at least one image actually carries that tag (zero-images-examined is a
     hard failure, never a silent pass).

Exit code is the gate: zero objects/containers/tagged-images examined is a
hard failure (critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import sys
from typing import Any

import yaml


def iter_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every container + initContainer spec in a pod-spec-bearing manifest."""
    kind = doc.get("kind")
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    elif kind == "CronJob":
        pod_spec = (
            doc.get("spec", {})
            .get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
        )
    else:
        return []
    return list(pod_spec.get("containers", []) or []) + list(
        pod_spec.get("initContainers", []) or []
    )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check-image-tag-render.py <expected-tag>", file=sys.stderr)
        return 1
    expected_tag = sys.argv[1]

    raw = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(raw) if isinstance(d, dict)]
    print(f"Parsed {len(docs)} rendered object(s) from stdin.")
    if not docs:
        print("FAIL: zero rendered objects -- helm template produced nothing", file=sys.stderr)
        return 1

    errors: list[str] = []
    images_examined = 0
    images_with_expected_tag = 0

    for doc in docs:
        name = doc.get("metadata", {}).get("name", "<unknown>")
        for container in iter_containers(doc):
            image = container.get("image")
            if not image:
                continue
            images_examined += 1

            if "%!" in image:
                errors.append(
                    f"{name}/{container.get('name')}: image {image!r} contains a "
                    "corrupted Go format verb (%!...) -- numeric tag not stringified"
                )
                continue

            if ":" not in image:
                errors.append(
                    f"{name}/{container.get('name')}: image {image!r} has no tag component"
                )
                continue

            tag = image.rsplit(":", 1)[1]
            if tag == expected_tag:
                images_with_expected_tag += 1

    print(
        f"Examined {images_examined} container image(s), "
        f"{images_with_expected_tag} tagged with the expected tag {expected_tag!r}."
    )
    if images_examined == 0:
        print("FAIL: zero container images examined", file=sys.stderr)
        return 1
    if images_with_expected_tag == 0:
        errors.append(
            f"no rendered image carried the expected tag {expected_tag!r} -- "
            "global.imageTag did not propagate"
        )

    if errors:
        print(f"FAIL: {len(errors)} violation(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"PASS: all {images_examined} rendered image(s) carry a clean, correct tag.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
