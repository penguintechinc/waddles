#!/usr/bin/env python3
"""Legacy single-app env assertion, env-agnostic (fix/legacy-path-mutually-exclusive).

`scripts/ci/check-alpha-render-bundle-executor.py` (fix/alpha-deploy-executor-and-
failfast) already asserts no legacy single-app env var renders against
values-alpha.yaml specifically, but it hard-requires at least one bundle-executor
Deployment to be present (alpha-only: beta/gamma/production disable
`rustDataPlane.bundleExecutor{,Action}` and would never reach its legacy-env
assertion at all). This script carries the SAME legacy-env-name list but is a
standalone gate usable against ANY rendered manifest stream -- a bare
`helm template` with no env-specific values file (the base `values.yaml`
defaults every operator inherits before choosing an environment) and every
`values-{alpha,beta,gamma,production}.yaml` topology alike.

Regression this guards: alpha 2026-10-03 -- a legacy single-app consumer
(driven by PROCESS_APP_ID/ACTION_APP_ID) competed in the same Valkey consumer
group as the multi-tenant one for `waddles.core.example.ping`, intermittently
dead-lettering `!ping`. The chart-side half of the fix is these env vars never
rendering unless an operator explicitly sets the driving value -- this script
is the regression gate for that half.

Exit code is the gate: zero containers examined is a hard failure, never a
silent pass (critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import sys
from typing import Any

import yaml

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
    """Return every container + initContainer spec in a Pod-shaped manifest
    (Deployment/StatefulSet/DaemonSet/Job/CronJob all nest `spec.template.spec`,
    CronJob one level deeper -- handled by the caller's `kind` filter instead
    of duplicating the extra nesting here)."""
    pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    return list(pod_spec.get("containers", []) or []) + list(
        pod_spec.get("initContainers", []) or []
    )


def main() -> int:
    raw = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(raw) if isinstance(d, dict)]
    print(f"Parsed {len(docs)} rendered object(s) from stdin.")
    if not docs:
        print("FAIL: zero rendered objects -- helm template produced nothing", file=sys.stderr)
        return 1

    errors: list[str] = []
    containers_examined = 0
    env_names_examined = 0

    for doc in docs:
        kind = doc.get("kind")
        if kind == "CronJob":
            pod_spec = (
                doc.get("spec", {})
                .get("jobTemplate", {})
                .get("spec", {})
                .get("template", {})
                .get("spec", {})
            )
            containers = list(pod_spec.get("containers", []) or []) + list(
                pod_spec.get("initContainers", []) or []
            )
        elif kind in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
            containers = iter_containers(doc)
        else:
            continue

        name = doc.get("metadata", {}).get("name", "<unknown>")
        for container in containers:
            containers_examined += 1
            for env_entry in container.get("env", []) or []:
                env_name = env_entry.get("name")
                if env_name:
                    env_names_examined += 1
                if env_name in LEGACY_ENV_NAMES:
                    errors.append(
                        f"{kind}/{name}/{container.get('name')}: legacy single-app env "
                        f"{env_name} rendered -- must be absent unless an operator "
                        "explicitly set the driving value"
                    )

    print(f"Examined {containers_examined} container(s), {env_names_examined} env var name(s).")
    if containers_examined == 0:
        print("FAIL: zero containers examined", file=sys.stderr)
        return 1

    if errors:
        print(f"FAIL: {len(errors)} violation(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("PASS: no legacy single-app env var rendered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
