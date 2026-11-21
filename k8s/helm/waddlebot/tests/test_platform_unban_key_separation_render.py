"""Helm-template assertions for the Discord-native unban sync key separation.

svc-ingest signs platform-unban events with a DEDICATED Ed25519 key; svc-action must hold the PUBLIC
half only (it can verify, never mint an event) and no other workload may hold either. The key is
auto-provisioned create-if-absent by the auto-provision-keys hook. Zero rendered documents / no
matching workload is a hard failure, never a silent pass; a missing ``helm`` is a skip locally but
a FAILURE in CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"
_SIGNING = "PLATFORM_UNBAN_SIGNING_KEY"
_PUBLIC = "PLATFORM_UNBAN_PUBLIC_KEYS"


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    """Alpha render with the Rust data plane (svc-ingest, svc-action) on."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    argv = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_ALPHA_VALUES),
        "--set",
        "pipeline.rustDataPlane.enabled=true",
        "--set",
        "pipeline.rustDataPlane.svcIngest.enabled=true",
        "--set",
        "pipeline.rustDataPlane.svcAction.enabled=true",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert rendered, "helm template produced zero documents -- cannot be a pass"
    return rendered


def _env_names(doc: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for container in doc["spec"]["template"]["spec"].get("containers", []):
        names.update(e["name"] for e in container.get("env", []))
    return names


def _workload(docs: list[dict[str, Any]], suffix: str) -> dict[str, Any]:
    found = [
        d
        for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith(suffix)
    ]
    assert len(found) == 1, f"expected exactly one Deployment ending {suffix!r}, got {len(found)}"
    return found[0]


def test_only_svc_ingest_holds_the_signing_key_and_svc_action_the_public_half(
    docs: list[dict[str, Any]],
) -> None:
    ingest = _env_names(_workload(docs, "svc-ingest-rust"))
    action = _env_names(_workload(docs, "svc-action-rust"))
    assert _SIGNING in ingest and "PLATFORM_UNBAN_KEY_ID" in ingest
    assert _PUBLIC not in ingest, "the signer must not need the verify ring"
    assert _PUBLIC in action
    assert _SIGNING not in action, "svc-action must never hold the signing key"


def test_no_other_workload_holds_either_half(docs: list[dict[str, Any]]) -> None:
    holders = set()
    for doc in docs:
        if doc.get("kind") not in {"Deployment", "StatefulSet", "Job", "CronJob"}:
            continue
        spec = doc["spec"]
        pod = (
            spec["jobTemplate"]["spec"]["template"]["spec"]
            if doc["kind"] == "CronJob"
            else spec["template"]["spec"]
        )
        env = {e["name"] for c in pod.get("containers", []) for e in c.get("env", [])}
        if env & {_SIGNING, _PUBLIC}:
            holders.add(doc["metadata"]["name"])
    assert len(holders) == 2, f"only svc-ingest and svc-action may reference the key: {holders}"
    assert all(h.endswith(("svc-ingest-rust", "svc-action-rust")) for h in holders)


def test_the_key_is_auto_provisioned_create_if_absent_with_a_public_configmap(
    docs: list[dict[str, Any]],
) -> None:
    jobs = [
        d
        for d in docs
        if d.get("kind") == "Job" and d["metadata"]["name"].endswith("auto-provision-keys")
    ]
    assert len(jobs) == 1
    script = "\n".join(
        c["args"][0] for c in jobs[0]["spec"]["template"]["spec"]["containers"] if c.get("args")
    )
    assert "provision_ed25519 \"${PLATFORM_UNBAN_SECRET}\"" in script
    assert "PLATFORM_UNBAN_PRIVATE_KEY PLATFORM_UNBAN_KEY_ID PLATFORM_UNBAN_PUBLIC_KEY" in script
    roles = [d for d in docs if d.get("kind") == "Role" and "auto-provision-keys" in d["metadata"]["name"]]
    assert roles, "the hook's Role must exist"
    names = {n for r in roles for rule in r["rules"] for n in rule.get("resourceNames", [])}
    assert any(n.endswith("platform-unban-signing") for n in names)
    assert any(n.endswith("platform-unban-signing-public") for n in names)
