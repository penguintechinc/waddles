#!/usr/bin/env python3
"""regression: bundle publish/fetch bucket split after SeaweedFS migration (#508/#509).

hub-api's `BUNDLE_BUCKET_NAME` (hub_api/services/storage_service.py's bundle
component/sidecar uploads) and core/bundle_executor's own `BUNDLE_BUCKET_NAME` env var MUST
resolve to the identical bucket, and the rendered SeaweedFS `s3-identities.json` MUST grant
the "hub-api" identity Write on that same bucket -- otherwise a bundle publish "succeeds" but
the executor 404s fetching it (`hub-api.yaml`'s identity used to be scoped to
`S3_BUCKET_NAME`/"waddlebot-assets" only, a different bucket entirely).

Reads a `helm template` multi-document YAML stream on stdin, same convention as
`check-helm-rendered-manifest-invariants.py`. Exit code is the gate; zero objects examined is
a hard failure, never a silent pass (critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import sys
from typing import Any

import yaml


def _env_value(containers: list[dict[str, Any]], name: str) -> str | None:
    """The literal `value` of env var `name` on the first container that declares it."""
    for container in containers:
        for env in container.get("env", []) or []:
            if isinstance(env, dict) and env.get("name") == name and "value" in env:
                return env.get("value")
    return None


def _configmap_refs(containers: list[dict[str, Any]]) -> set[str]:
    """Every ConfigMap name any container's `envFrom` references."""
    names: set[str] = set()
    for container in containers:
        for src in container.get("envFrom", []) or []:
            ref = (src or {}).get("configMapRef") or {}
            if ref.get("name"):
                names.add(ref["name"])
    return names


def main() -> int:
    raw = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(raw) if d]

    configmap_bucket: str | None = None
    configmap_name: str | None = None
    hub_api_configmap_refs: set[str] = set()
    executor_bucket: str | None = None
    seaweedfs_command: str | None = None
    objects_examined = 0

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata", {}) or {}
        labels = meta.get("labels", {}) or {}

        if kind == "ConfigMap" and "BUNDLE_BUCKET_NAME" in (doc.get("data") or {}):
            objects_examined += 1
            configmap_bucket = doc["data"]["BUNDLE_BUCKET_NAME"]
            configmap_name = meta.get("name")

        if kind != "Deployment":
            continue
        component = labels.get("app.kubernetes.io/component", "")
        containers = (
            doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []
        )

        if component == "hub-api":
            objects_examined += 1
            hub_api_configmap_refs = _configmap_refs(containers)

        if component == "bundle-executor":
            objects_examined += 1
            executor_bucket = _env_value(containers, "BUNDLE_BUCKET_NAME")

        if meta.get("name", "").endswith("seaweedfs") or labels.get(
            "app.kubernetes.io/name"
        ) == "seaweedfs":
            for container in containers:
                for cmd_part in container.get("command", []) or []:
                    if isinstance(cmd_part, str) and '"name": "hub-api"' in cmd_part:
                        objects_examined += 1
                        seaweedfs_command = cmd_part

    print(
        f"Examined {objects_examined} relevant object(s) "
        f"(ConfigMap, hub-api Deployment, bundle-executor Deployment, seaweedfs Deployment)."
    )
    if objects_examined == 0:
        print(
            "FAIL: zero objects examined -- this script is pointed at the wrong rendered "
            "manifest set (a zero denominator is a failure, never a silent pass).",
            file=sys.stderr,
        )
        return 1

    errors: list[str] = []
    if not configmap_bucket:
        errors.append("no ConfigMap with a BUNDLE_BUCKET_NAME key found")
    if configmap_name not in hub_api_configmap_refs:
        errors.append(
            f"hub-api Deployment's envFrom does not reference ConfigMap {configmap_name!r} "
            f"(found: {sorted(hub_api_configmap_refs)!r}) -- it would never see BUNDLE_BUCKET_NAME"
        )
    if not executor_bucket:
        errors.append("bundle-executor Deployment has no BUNDLE_BUCKET_NAME env var")
    if configmap_bucket and executor_bucket and configmap_bucket != executor_bucket:
        errors.append(
            f"hub-api's ConfigMap BUNDLE_BUCKET_NAME={configmap_bucket!r} != "
            f"bundle-executor BUNDLE_BUCKET_NAME={executor_bucket!r}"
        )
    if seaweedfs_command is None:
        errors.append("could not find the seaweedfs Deployment's hub-api identity block")
    elif configmap_bucket:
        for action in ("Read", "Write", "List"):
            token = f'"{action}:{configmap_bucket}"'
            if token not in seaweedfs_command:
                errors.append(
                    f"seaweedfs hub-api identity is missing {action} on bucket "
                    f"{configmap_bucket!r} ({token} not found in rendered s3-identities.json)"
                )

    if errors:
        print(f"FAIL: {len(errors)} invariant violation(s) found:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"PASS: hub-api (via ConfigMap {configmap_name!r}) and bundle-executor both target "
        f"BUNDLE_BUCKET_NAME={configmap_bucket!r}, hub-api identity has Read/Write/List on it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
