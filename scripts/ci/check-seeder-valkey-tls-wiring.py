#!/usr/bin/env python3
"""regression: seeder VALKEY_URL REPLACE_ME / missing TLS wiring.

The core-bundle-seeder Job (and hub-api, which calls the same
`valkey_admin_client.build_client()` consumer-group code path) used to get
`VALKEY_URL` ONLY from `envFrom`'s chart Secret -- a plaintext `redis://` URL
built from `REDIS_PASSWORD`, which on a stale/pre-fix cluster Secret can be a
literal `REPLACE_ME*` placeholder. `hub_api/services/valkey_admin_client.py`
defaults `SECURITY_TRANSPORT_TLS=true` and refuses any non-`rediss://` URL at
connect time, so the Job crash-looped with `ValueError: VALKEY_URL must use
rediss://...`.

This script renders `values-alpha.yaml` (where Valkey TLS material is real and
valid, i.e. `waddlebot.valkeyTlsMaterialAvailable` is true) and asserts, for
every Deployment/Job:

1. The EFFECTIVE env (envFrom merged with explicit `env:`, explicit wins on a
   name collision -- Kubernetes' own resolution order) never resolves to a
   literal value containing `REPLACE_ME`, whether that value came from a
   plain `env[].value`, a `secretKeyRef`/`configMapKeyRef`, or an
   `envFrom` Secret/ConfigMap -- i.e. a placeholder can never reach a running
   pod regardless of which layer introduced it.
2. The core-bundle-seeder Job and the hub-api Deployment mount the same
   `valkey-ca` CA volume the Rust data-plane Deployments
   (svc-process-rust/svc-ingest-rust/svc-action-rust) mount, whenever at
   least one of those Rust Deployments mounts it -- TLS wiring parity, not
   just a REPLACE_ME scan.

Reads a `helm template` multi-document YAML stream on stdin, same convention
as check-bundle-bucket-wiring.py. Exit code is the gate; zero objects
examined is a hard failure, never a silent pass (critical-rules.md
Verification Integrity). Never reads live cluster Secret data -- static
render only.
"""

from __future__ import annotations

import base64
import binascii
import sys
from typing import Any

import yaml

_RUST_DATA_PLANE_COMPONENTS = frozenset(
    {"svc-process-rust", "svc-ingest-rust", "svc-action-rust"}
)
_WIRING_REQUIRED_COMPONENTS = frozenset({"core-bundle-seeder", "hub-api"})


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    kind = doc.get("kind", "")
    if kind == "CronJob":
        return (
            doc.get("spec", {}).get("jobTemplate", {}).get("spec", {}).get("template", {}).get(
                "spec"
            )
            or {}
        )
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return doc.get("spec", {}).get("template", {}).get("spec") or {}
    return {}


def _decode(field: str, value: Any) -> str | None:
    """Plaintext for a Secret/ConfigMap data entry (base64 for Secret `data`)."""
    if not isinstance(value, str):
        return None
    if field == "data":
        try:
            return base64.b64decode(value, validate=True).decode("utf-8", errors="replace")
        except (binascii.Error, UnicodeDecodeError):
            return "<undecodable>"
    return value


def _index_configmaps_and_secrets(
    docs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """name -> {key: plaintext value} for every rendered ConfigMap/Secret."""
    configmaps: dict[str, dict[str, str]] = {}
    secrets: dict[str, dict[str, str]] = {}
    for doc in docs:
        kind = doc.get("kind")
        name = doc.get("metadata", {}).get("name")
        if not name or kind not in {"ConfigMap", "Secret"}:
            continue
        values: dict[str, str] = {}
        for field in ("data", "stringData"):
            for key, raw in (doc.get(field) or {}).items():
                decoded = _decode(field, raw)
                if decoded is not None:
                    values[key] = decoded
        (configmaps if kind == "ConfigMap" else secrets)[name] = values
    return configmaps, secrets


def _effective_container_env(
    container: dict[str, Any],
    configmaps: dict[str, dict[str, str]],
    secrets: dict[str, dict[str, str]],
) -> dict[str, str | None]:
    """The env a running pod would actually see: envFrom first, then explicit `env:`
    overriding same-named keys -- matches Kubernetes' own resolution order."""
    effective: dict[str, str | None] = {}
    for src in container.get("envFrom", []) or []:
        cm_ref = (src or {}).get("configMapRef") or {}
        sec_ref = (src or {}).get("secretRef") or {}
        if cm_ref.get("name"):
            effective.update(configmaps.get(cm_ref["name"], {}))
        if sec_ref.get("name"):
            effective.update(secrets.get(sec_ref["name"], {}))

    for env in container.get("env", []) or []:
        name = env.get("name")
        if not name:
            continue
        if "value" in env:
            effective[name] = env.get("value")
            continue
        value_from = env.get("valueFrom") or {}
        sec_key_ref = value_from.get("secretKeyRef")
        cm_key_ref = value_from.get("configMapKeyRef")
        if sec_key_ref:
            effective[name] = secrets.get(sec_key_ref.get("name", ""), {}).get(
                sec_key_ref.get("key", "")
            )
        elif cm_key_ref:
            effective[name] = configmaps.get(cm_key_ref.get("name", ""), {}).get(
                cm_key_ref.get("key", "")
            )
        else:
            effective[name] = None  # fieldRef/resourceFieldRef/secretRef -- not a literal
    return effective


def _has_valkey_ca_mount(pod_spec: dict[str, Any]) -> bool:
    volumes = {v.get("name") for v in pod_spec.get("volumes", []) or []}
    if "valkey-ca" not in volumes:
        return False
    for container in pod_spec.get("containers", []) or []:
        mounts = {m.get("name") for m in container.get("volumeMounts", []) or []}
        if "valkey-ca" in mounts:
            return True
    return False


def main() -> int:
    raw = sys.stdin.read()
    docs = [d for d in yaml.safe_load_all(raw) if isinstance(d, dict)]

    configmaps, secrets = _index_configmaps_and_secrets(docs)

    objects_examined = 0
    placeholder_findings: list[str] = []
    rust_ca_mounted = False
    component_ca_status: dict[str, bool] = {}

    for doc in docs:
        kind = doc.get("kind", "")
        if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}:
            continue
        pod_spec = _pod_spec(doc)
        if not pod_spec:
            continue
        name = doc.get("metadata", {}).get("name", "<unnamed>")
        component = (doc.get("metadata", {}).get("labels") or {}).get(
            "app.kubernetes.io/component", ""
        )
        objects_examined += 1

        for containers_key in ("containers", "initContainers"):
            for container in pod_spec.get(containers_key, []) or []:
                effective = _effective_container_env(container, configmaps, secrets)
                for env_name, value in effective.items():
                    if isinstance(value, str) and "REPLACE_ME" in value:
                        placeholder_findings.append(
                            f"{kind}/{name} container {container.get('name')} "
                            f"effective env {env_name}: resolves to a REPLACE_ME "
                            f"placeholder -- would reach a running pod"
                        )

        if component in _RUST_DATA_PLANE_COMPONENTS:
            rust_ca_mounted = rust_ca_mounted or _has_valkey_ca_mount(pod_spec)
        if component in _WIRING_REQUIRED_COMPONENTS:
            component_ca_status[component] = _has_valkey_ca_mount(pod_spec)

    print(f"Examined {objects_examined} Deployment/StatefulSet/DaemonSet/Job/CronJob object(s).")
    if objects_examined == 0:
        print(
            "FAIL: zero objects examined -- this script is pointed at the wrong rendered "
            "manifest set (a zero denominator is a failure, never a silent pass).",
            file=sys.stderr,
        )
        return 1

    errors = list(placeholder_findings)
    if rust_ca_mounted:
        for component in sorted(_WIRING_REQUIRED_COMPONENTS):
            if component not in component_ca_status:
                errors.append(
                    f"expected a {component!r}-labeled workload in this render but found none"
                )
            elif not component_ca_status[component]:
                errors.append(
                    f"{component}: Rust data-plane Deployments mount the valkey-ca CA volume "
                    f"but this workload does not -- TLS wiring parity gap"
                )

    if errors:
        print(f"FAIL: {len(errors)} invariant violation(s) found:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        "PASS: no REPLACE_ME placeholder reaches a running pod's effective env, and "
        "core-bundle-seeder/hub-api carry the same valkey-ca TLS wiring as the Rust "
        "data-plane Deployments."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
