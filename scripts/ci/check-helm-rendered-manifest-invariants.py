#!/usr/bin/env python3
"""Static check for two apply-time-only Kubernetes API validation failures that
`helm lint` and `helm template` cannot catch (neither runs the rendered YAML
through API validation) and that `kubeconform` also cannot catch (both are
business-logic constraints the OpenAPI schema itself does not encode -- the
schema happily allows two `Service.spec.ports[]` entries with the same `name`,
and an `EnvVar` with both `value` and `valueFrom` set):

1. Duplicate `.spec.ports[].name` within a single Service (or a Deployment/
   StatefulSet/DaemonSet/Job/CronJob container's own `.ports[].name`) --
   rejected by the API server with e.g.
   `spec.ports[1].name: Duplicate value: "http"`.
2. A container `env[]` entry with BOTH `value` (non-empty) and `valueFrom`
   set -- rejected by the API server with
   'env[N].valueFrom: Invalid value: "": may not be specified when `value`
   is not empty'.

Reads a `helm template` multi-document YAML stream on stdin. Exit code is the
gate: zero objects examined is treated as a hard failure, never a silent pass
(see critical-rules.md Verification Integrity).
"""
from __future__ import annotations

import sys
from typing import Any

import yaml

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


def iter_containers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield every container spec (init + regular) in a workload manifest.

    CronJob nests one level deeper (spec.jobTemplate.spec.template...) than
    the other four workload kinds -- handled explicitly rather than assumed
    identical.
    """
    kind = doc.get("kind")
    if kind == "CronJob":
        pod_spec = (
            doc.get("spec", {})
            .get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
        )
    else:
        pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    return list(pod_spec.get("containers", []) or []) + list(
        pod_spec.get("initContainers", []) or []
    )


def check_duplicate_port_names(name_label: str, ports: list[Any]) -> list[str]:
    """Return one message per port name that appears more than once in `ports`."""
    errors = []
    seen: dict[str, int] = {}
    for i, p in enumerate(ports or []):
        if not isinstance(p, dict):
            continue
        pname = p.get("name")
        if pname is None:
            continue
        if pname in seen:
            errors.append(
                f"{name_label}: spec.ports[{i}].name duplicates "
                f"spec.ports[{seen[pname]}].name: {pname!r}"
            )
        else:
            seen[pname] = i
    return errors


def check_env_value_and_value_from(name_label: str, containers: list[dict[str, Any]]) -> list[str]:
    """Return one message per env entry setting both `value` (non-empty) and `valueFrom`."""
    errors = []
    for c in containers:
        cname = c.get("name", "<unnamed>")
        for i, e in enumerate(c.get("env", []) or []):
            if not isinstance(e, dict):
                continue
            value = e.get("value")
            value_from = e.get("valueFrom")
            if value_from is not None and value is not None and value != "":
                errors.append(
                    f"{name_label} container {cname!r}: env[{i}] {e.get('name')!r} "
                    f"sets both value={value!r} and valueFrom -- API server rejects "
                    f"this with \"may not be specified when `value` is not empty\""
                )
    return errors


def main() -> int:
    raw = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(raw) if d]

    services_examined = 0
    workloads_examined = 0
    containers_examined = 0
    all_errors: list[str] = []

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata", {}) or {}
        label = f"{kind}/{meta.get('name', '<unnamed>')} (ns={meta.get('namespace', '<default>')})"

        if kind == "Service":
            services_examined += 1
            all_errors.extend(
                check_duplicate_port_names(label, doc.get("spec", {}).get("ports", []))
            )

        if kind in WORKLOAD_KINDS:
            workloads_examined += 1
            containers = iter_containers(doc)
            containers_examined += len(containers)
            for c in containers:
                if not isinstance(c, dict):
                    continue
                all_errors.extend(
                    check_duplicate_port_names(
                        f"{label} container {c.get('name', '<unnamed>')!r}",
                        c.get("ports", []),
                    )
                )
            all_errors.extend(check_env_value_and_value_from(label, containers))

    total_examined = services_examined + workloads_examined
    print(
        f"Examined {services_examined} Service object(s), {workloads_examined} "
        f"workload object(s) ({containers_examined} container(s) total)."
    )

    if total_examined == 0:
        print(
            "FAIL: zero objects examined -- the input stream is empty or this "
            "script is pointed at the wrong rendered manifest set (a zero "
            "denominator is a failure, never a silent pass).",
            file=sys.stderr,
        )
        return 1

    if all_errors:
        print(f"FAIL: {len(all_errors)} invariant violation(s) found:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"PASS: 0 violations across {total_examined} object(s) examined.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
