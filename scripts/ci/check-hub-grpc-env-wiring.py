#!/usr/bin/env python3
"""Render assertions for feature/hub-grpc-env-wiring.

Reads a `helm template waddlebot k8s/helm/waddlebot --values values-alpha.yaml
--set pipeline.rustDataPlane.enabled=true` multi-document YAML stream on
stdin and asserts that both `svc-process-rust` and `svc-action-rust`
Deployments render a non-empty `HUB_API_GRPC_ENDPOINT`, non-empty
`SERVICE_JWT_TOKEN_ENDPOINT`, and `SERVICE_JWT_SA_TOKEN_PATH` pointed at the
audience-scoped projected bootstrap token this pod already mounts (NOT the
generic default k8s ServiceAccount token path, which lacks the audience
hub-api's TokenReview call requires).

PR #561 lands PII tokenization default-ON; PR #569 makes `svc_process`/
`svc_action` fail loud (exit non-zero, crash-loop) at startup if
`HUB_API_GRPC_ENDPOINT`/`SERVICE_JWT_TOKEN_ENDPOINT` are unset while
tokenization is enabled. This check is the chart-side guarantee that never
happens by default.

Exit code is the gate: zero Deployments/env entries examined is a hard
failure, never a silent pass (critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import sys
from typing import Any

import yaml

EXPECTED_COMPONENTS = {"svc-process-rust", "svc-action-rust"}
EXPECTED_SA_TOKEN_PATH = "/var/run/secrets/waddlebot/service-jwt/service-jwt-bootstrap-token"
REQUIRED_ENV_NAMES = {
    "HUB_API_GRPC_ENDPOINT",
    "SERVICE_JWT_TOKEN_ENDPOINT",
    "SERVICE_JWT_SA_TOKEN_PATH",
}


def iter_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every container spec in a Deployment's pod template."""
    pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    return list(pod_spec.get("containers", []) or [])


def component_label(doc: dict[str, Any]) -> str | None:
    return doc.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")


def main() -> int:
    raw = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(raw) if isinstance(d, dict)]
    print(f"Parsed {len(docs)} rendered object(s) from stdin.")
    if not docs:
        print("FAIL: zero rendered objects -- helm template produced nothing", file=sys.stderr)
        return 1

    errors: list[str] = []
    deployments_found = 0
    env_entries_examined = 0

    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        comp = component_label(doc)
        if comp not in EXPECTED_COMPONENTS:
            continue
        deployments_found += 1
        name = doc.get("metadata", {}).get("name", "<unknown>")

        containers = iter_containers(doc)
        if not containers:
            errors.append(f"{name}: no containers found")
            continue

        found: dict[str, str] = {}
        for container in containers:
            for env_entry in container.get("env", []) or []:
                env_name = env_entry.get("name")
                if env_name in REQUIRED_ENV_NAMES:
                    env_entries_examined += 1
                    found[env_name] = env_entry.get("value", "")

        for required in REQUIRED_ENV_NAMES:
            if required not in found:
                errors.append(f"{name}: {required} is not rendered at all")
            elif not found[required]:
                errors.append(f"{name}: {required} rendered empty -- would fail loud at startup")

        if found.get("SERVICE_JWT_SA_TOKEN_PATH") not in (None, EXPECTED_SA_TOKEN_PATH):
            errors.append(
                f"{name}: SERVICE_JWT_SA_TOKEN_PATH={found.get('SERVICE_JWT_SA_TOKEN_PATH')!r} "
                f"!= expected {EXPECTED_SA_TOKEN_PATH!r} (wrong-audience SA token "
                "would be rejected by hub-api's TokenReview call)"
            )

    print(
        f"Examined {deployments_found} svc-process-rust/svc-action-rust Deployment(s), "
        f"{env_entries_examined} matching env entr(ies)."
    )
    if deployments_found == 0:
        print(
            "FAIL: zero svc-process-rust/svc-action-rust Deployments found -- "
            "pipeline.rustDataPlane.enabled off, or component label changed",
            file=sys.stderr,
        )
        return 1
    if env_entries_examined == 0:
        print("FAIL: zero matching env entries examined", file=sys.stderr)
        return 1

    if errors:
        print(f"FAIL: {len(errors)} violation(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("PASS: HUB_API_GRPC_ENDPOINT/SERVICE_JWT_* render non-empty on both Deployments.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
