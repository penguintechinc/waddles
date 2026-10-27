"""Helm-template assertions for ban-evasion suppression (hub-api env).

`hub_api/services/suppression_keys.py` derives every suppression code from `K_master`, the platform
tenant KEK (`TENANT_KEK_HEX`). Two properties are chart-level and pinned here:

1. hub-api receives it from the `autoProvisionedKeys.tenantKek` Secret (`optional: true`, like the
   SSO key: a deployment that never erases a banned user must still start; the code paths that
   need the key fail loudly instead), plus the optional retention ceiling.
2. NO other workload mounts that Secret or the variable. hub-api is the PII boundary; the codes
   stored in `suppression_records` are only useless to a database thief because the key lives
   nowhere else.

Zero rendered documents / zero containers scanned is a hard failure, never a silent pass
(critical-rules.md Verification Integrity).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


def _render(extra: list[str] | None = None) -> list[dict[str, Any]]:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    cmd = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0",
        "--values", str(_ALPHA_VALUES),
        *(extra or []),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


def _pod_spec(doc: dict[str, Any]) -> dict[str, Any]:
    spec = doc.get("spec", {})
    if doc["kind"] == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def _containers(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    found = []
    for doc in docs:
        if doc.get("kind") not in _WORKLOAD_KINDS:
            continue
        pod = _pod_spec(doc)
        for container in [*pod.get("containers", []), *pod.get("initContainers", [])]:
            found.append((doc["metadata"]["name"], container))
    return found


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    return _render()


def _is_hub_api(workload: str, container: dict[str, Any]) -> bool:
    """The hub-api Deployment (`<release>-hub-api-v3`)'s `hub-api` container."""
    return "-hub-api-" in f"{workload}-" and container["name"] == "hub-api"


def _hub_api(docs: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [c for name, c in _containers(docs) if _is_hub_api(name, c)]
    assert len(matches) == 1, f"expected exactly one hub-api container, got {len(matches)}"
    return matches[0]


def test_hub_api_gets_the_tenant_kek_from_the_platform_secret(docs: list[dict[str, Any]]) -> None:
    env = {e["name"]: e for e in _hub_api(docs).get("env", [])}
    ref = env["TENANT_KEK_HEX"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "waddlebot-tenant-kek", "key": "TENANT_KEK_HEX", "optional": True}


def test_the_retention_ceiling_is_absent_by_default(docs: list[dict[str, Any]]) -> None:
    names = {e["name"] for e in _hub_api(docs).get("env", [])}
    assert "WADDLES_SUPPRESSION_MAX_RETENTION_DAYS" not in names  # until lifted is the default


def test_the_retention_ceiling_is_passed_through_when_set() -> None:
    docs = _render(["--set", "suppression.maxRetentionDays=365"])
    env = {e["name"]: e for e in _hub_api(docs).get("env", [])}
    assert env["WADDLES_SUPPRESSION_MAX_RETENTION_DAYS"]["value"] == "365"


def test_no_other_workload_holds_the_tenant_kek(docs: list[dict[str, Any]]) -> None:
    scanned = 0
    leaks = []
    for workload, container in _containers(docs):
        scanned += 1
        if _is_hub_api(workload, container):
            continue
        text = yaml.safe_dump(container)
        if "TENANT_KEK_HEX" in text or "waddlebot-tenant-kek" in text:
            leaks.append(f"{workload}/{container['name']}")
    print(f"scanned {scanned} containers; leaks={leaks}")
    assert scanned > 5, "scanned suspiciously few containers -- the scan did not run"
    assert leaks == [], f"the tenant KEK must stay inside hub-api, but: {leaks}"
    # Also not injected wholesale via envFrom into anything but hub-api.
    for workload, container in _containers(docs):
        if _is_hub_api(workload, container):
            continue
        for source in container.get("envFrom", []):
            assert source.get("secretRef", {}).get("name") != "waddlebot-tenant-kek", workload
