#!/usr/bin/env python3
"""Render assertions for fix/alpha-deploy-executor-and-failfast.

Reads a `helm template waddlebot k8s/helm/waddlebot --values values-alpha.yaml
--set global.imageTag=<SHA>` multi-document YAML stream on stdin and asserts:

1. Both bundle-executor Deployments (bundle-executor, bundle-executor-action)
   render with the OOM-fix resources (request 1Gi/500m, limit 2560Mi/2000m
   CPU) -- alpha was OOMKilled at the old 128Mi/64Mi tier compiling a 21MB
   CPython component plus a .NET component via Cranelift.
2. Both bundle-executor Deployments' startupProbe failureThreshold gives a
   cold compile up to ~5 minutes (>= 30 at periodSeconds=5), not the old 12
   (62s).
3. Every container image referencing the bundle-executor repository uses the
   SHA tag this run was templated with -- never the old static `:alpha` tag
   (the bug: nothing ever rebuilt it, so both executors ran a stale build).
4. No legacy single-bundle env var (PROCESS_APP_ID, PROCESS_INGEST_*,
   PROCESS_BUNDLE_*, ACTION_APP_ID, ACTION_BUNDLE_*) appears anywhere in the
   alpha render -- the DB-driven multi-tenant active-bundle loader is the
   sole bundle-selection path now.

Exit code is the gate: zero objects/containers examined is a hard failure,
never a silent pass (critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import sys
from typing import Any

import yaml

EXECUTOR_COMPONENTS = {"bundle-executor", "bundle-executor-action"}
EXPECTED_RESOURCES = {
    "requests": {"cpu": "500m", "memory": "1Gi"},
    "limits": {"cpu": "2000m", "memory": "2560Mi"},
}
MIN_STARTUP_FAILURE_THRESHOLD = 30  # periodSeconds=5 * 30 = 150s+initialDelay, generous floor
LEGACY_ENV_NAMES = {
    "PROCESS_APP_ID",
    "PROCESS_INGEST_PLATFORM",
    "PROCESS_INGEST_SOURCE_ID",
    "PROCESS_BUNDLE_DIGEST",
    "PROCESS_BUNDLE_VERSION",
    "PROCESS_BUNDLE_COMPONENT_KEY",
    "PROCESS_BUNDLE_SIDECAR_KEY",
    "ACTION_APP_ID",
    "ACTION_BUNDLE_DIGEST",
    "ACTION_BUNDLE_VERSION",
    "ACTION_BUNDLE_COMPONENT_KEY",
    "ACTION_BUNDLE_SIDECAR_KEY",
}


def iter_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every container + initContainer spec in a Deployment manifest."""
    pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    return list(pod_spec.get("containers", []) or []) + list(
        pod_spec.get("initContainers", []) or []
    )


def component_label(doc: dict[str, Any]) -> str | None:
    return (
        doc.get("metadata", {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
    )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check-alpha-render-bundle-executor.py <expected-sha-tag>", file=sys.stderr)
        return 1
    expected_tag = sys.argv[1]

    raw = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(raw) if isinstance(d, dict)]
    print(f"Parsed {len(docs)} rendered object(s) from stdin.")
    if not docs:
        print("FAIL: zero rendered objects -- helm template produced nothing", file=sys.stderr)
        return 1

    errors: list[str] = []
    executor_deployments_found = 0
    containers_examined = 0
    env_names_examined = 0

    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        comp = component_label(doc)
        name = doc.get("metadata", {}).get("name", "<unknown>")

        for container in iter_containers(doc):
            containers_examined += 1
            for env_entry in container.get("env", []) or []:
                env_name = env_entry.get("name")
                if env_name:
                    env_names_examined += 1
                if env_name in LEGACY_ENV_NAMES:
                    errors.append(
                        f"{name}/{container.get('name')}: legacy env {env_name} "
                        "rendered -- must be absent from the alpha render"
                    )

        if comp not in EXECUTOR_COMPONENTS:
            continue
        executor_deployments_found += 1

        containers = iter_containers(doc)
        if not containers:
            errors.append(f"{name}: no containers found")
            continue
        container = containers[0]

        resources = container.get("resources", {})
        if resources != EXPECTED_RESOURCES:
            errors.append(
                f"{name}: resources {resources!r} != expected {EXPECTED_RESOURCES!r}"
            )

        image = container.get("image", "")
        if "bundle-executor" in image:
            if ":" not in image or image.rsplit(":", 1)[1] != expected_tag:
                errors.append(
                    f"{name}: image {image!r} does not use expected SHA tag {expected_tag!r}"
                )

        startup = container.get("startupProbe", {})
        threshold = startup.get("failureThreshold")
        if not isinstance(threshold, int) or threshold < MIN_STARTUP_FAILURE_THRESHOLD:
            errors.append(
                f"{name}: startupProbe.failureThreshold={threshold!r} "
                f"< minimum {MIN_STARTUP_FAILURE_THRESHOLD} (cold-compile budget regression)"
            )

    print(
        f"Examined {executor_deployments_found} bundle-executor Deployment(s), "
        f"{containers_examined} container(s), {env_names_examined} env var name(s)."
    )
    if executor_deployments_found == 0:
        print(
            "FAIL: zero bundle-executor Deployments found -- "
            "rustDataPlane.bundleExecutor{,Action}.enabled off, or component label changed",
            file=sys.stderr,
        )
        return 1
    if containers_examined == 0:
        print("FAIL: zero containers examined", file=sys.stderr)
        return 1

    if errors:
        print(f"FAIL: {len(errors)} violation(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("PASS: bundle-executor resources/probes/image-tag/legacy-env assertions hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
