"""Helm-render assertions: ``keys:tenant-dek:read`` reaches ONLY the data-plane identities.

The tenant-DEK broker (hub_api ``POST /api/v1/internal/keys/tenant-dek``) releases an unwrapped
tenant key, so its machine-JWT scope is granted to svc-process / svc-action / svc-ingest alone --
never svc-presentation or any other identity. Zero identities rendered is a hard failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA_VALUES = _CHART_DIR / "values-alpha.yaml"
SCOPE = "keys:tenant-dek:read"
DATA_PLANE = {"svc-process", "svc-action", "svc-ingest"}


@pytest.fixture(scope="module")
def identities() -> dict[str, list[str]]:
    """Render alpha (Rust data plane on, dynamicConnections on) and map service -> scopes."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    argv = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0", "--values", str(_ALPHA_VALUES),
        "--set", "pipeline.rustDataPlane.enabled=true",
        "--set", "pipeline.rustDataPlane.svcIngest.enabled=true",
    ]  # fmt: skip
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    docs: list[dict[str, Any]] = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    hub = [
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("-hub-api-v3")
    ]  # fmt: skip
    assert len(hub) == 1
    raw = ""
    for container in hub[0]["spec"]["template"]["spec"]["containers"]:
        for item in container.get("env", []) or []:
            if item["name"] == "SERVICE_JWT_IDENTITIES":
                raw = item["value"]
    entries = json.loads(raw)
    assert entries, "zero identities rendered -- cannot be a pass"
    return {e["service_id"].rsplit("/", 1)[-1]: e["allowed_scopes"] for e in entries}


def test_every_data_plane_identity_holds_the_scope(identities: dict[str, list[str]]) -> None:
    """All three data-plane services can fetch a tenant DEK."""
    for service in DATA_PLANE:
        assert SCOPE in identities[service], service


def test_no_other_identity_holds_the_scope(identities: dict[str, list[str]]) -> None:
    """Nothing outside the data plane is ever granted DEK release."""
    holders = {name for name, scopes in identities.items() if SCOPE in scopes}
    assert holders == DATA_PLANE
