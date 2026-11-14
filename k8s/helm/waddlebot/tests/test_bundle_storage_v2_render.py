"""Helm-template assertions for bundle storage v2 wiring (docs/bundle-storage-v2.md).

Asserts, against the real rendered manifests, that:

1. Defaults (feature off) render no BUNDLE_DB_* / BUNDLE_MIGRATOR_PASSWORD env var on any
   workload, so existing values files keep rendering unchanged.
2. Enabled: svc-process-rust and svc-action-rust (ONLY those) get BUNDLE_DB_* with the password
   by secretKeyRef from the operator's existingSecret; hub-api (ONLY) gets BUNDLE_MIGRATOR_PASSWORD.
3. Enabled without an existingSecret fails loud at render time, never renders a default secret.

Zero matching containers is a hard failure, never a vacuous pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_VALUES = _CHART_DIR / "values-dal2alpha.yaml"
_DB_WORKLOADS = {"svc-process-rust", "svc-action-rust"}
_ENABLE = [
    "--set", "bundleStorageV2.db.enabled=true",
    "--set", "bundleStorageV2.db.existingSecret=bundle-db-ext",
    "--set", "bundleStorageV2.migrator.enabled=true",
    "--set", "bundleStorageV2.migrator.existingSecret=bundle-migrator-ext",
]


def _render(extra: list[str]) -> subprocess.CompletedProcess[str]:
    """Run helm template with the dal2alpha values plus extra args."""
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
         "--values", str(_VALUES), *extra],
        capture_output=True, text=True, check=False,
    )


def _containers(extra: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Map workload name -> its containers' env lists across all Deployments."""
    res = _render(extra)
    if res.returncode != 0:
        pytest.fail(f"helm template failed:\n{res.stderr}")
    out: dict[str, list[dict[str, Any]]] = {}
    for doc in yaml.safe_load_all(res.stdout):
        if not doc or doc.get("kind") != "Deployment":
            continue
        comp = doc["metadata"]["labels"].get("app.kubernetes.io/component", doc["metadata"]["name"])
        for c in doc["spec"]["template"]["spec"]["containers"]:
            out.setdefault(comp, []).extend(c.get("env", []) or [])
    assert out, "no Deployments rendered"
    return out


def _names(envs: list[dict[str, Any]]) -> set[str]:
    return {e["name"] for e in envs}


def test_default_renders_no_bundle_env() -> None:
    """Feature off: no workload carries the new env vars."""
    workloads = _containers([])
    for name, envs in workloads.items():
        leaked = {n for n in _names(envs) if n.startswith("BUNDLE_DB_") or n == "BUNDLE_MIGRATOR_PASSWORD"}
        assert not leaked, f"{name} leaked {leaked} with the feature off"


def test_enabled_scopes_secrets_to_owning_pods() -> None:
    """Feature on: db env only in the two Rust stages, migrator only in hub-api."""
    workloads = _containers(_ENABLE)
    assert _DB_WORKLOADS <= workloads.keys(), f"missing workloads: {_DB_WORKLOADS - workloads.keys()}"
    assert "hub-api" in workloads or any("hub-api" in k for k in workloads)
    for name, envs in workloads.items():
        by_name = {e["name"]: e for e in envs}
        if name in _DB_WORKLOADS:
            for var in ("BUNDLE_DB_HOST", "BUNDLE_DB_PORT", "BUNDLE_DB_NAME", "BUNDLE_DB_USER"):
                assert var in by_name, f"{name} missing {var}"
            ref = by_name["BUNDLE_DB_PASSWORD"]["valueFrom"]["secretKeyRef"]
            assert ref == {"name": "bundle-db-ext", "key": "BUNDLE_DB_PASSWORD"}
            assert "BUNDLE_MIGRATOR_PASSWORD" not in by_name
        elif "hub-api" in name:
            ref = by_name["BUNDLE_MIGRATOR_PASSWORD"]["valueFrom"]["secretKeyRef"]
            assert ref == {"name": "bundle-migrator-ext", "key": "BUNDLE_MIGRATOR_PASSWORD"}
            assert not any(n.startswith("BUNDLE_DB_") for n in by_name)
        else:
            assert "BUNDLE_MIGRATOR_PASSWORD" not in by_name
            assert not any(n.startswith("BUNDLE_DB_") for n in by_name), f"{name} got BUNDLE_DB_*"


@pytest.mark.parametrize("flag", ["db", "migrator"])
def test_enabled_without_existing_secret_fails_loud(flag: str) -> None:
    """Enabling without an existingSecret must fail the render, not default a secret."""
    res = _render(["--set", f"bundleStorageV2.{flag}.enabled=true"])
    assert res.returncode != 0
    assert f"bundleStorageV2.{flag}.existingSecret is required" in res.stderr
