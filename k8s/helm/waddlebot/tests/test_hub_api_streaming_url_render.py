"""hub-api gets its svc-streaming address from Helm, matching the Service it actually talks to.

Regression: `hub_api/services/calls_proxy.py` defaulted to `http://svc-streaming:8093` while the
chart serves svc-streaming on `pipeline.svcStreaming.port` (8208) under `<release>-svc-streaming`.
Zero matching Deployments/containers is a hard failure, never a silent pass.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(extra_set: list[str]) -> list[dict[str, Any]]:
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
            "--values", str(_CHART_DIR / "values-alpha.yaml")]
    for kv in extra_set:
        args += ["--set", kv]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _hub_api_env(docs: list[dict[str, Any]]) -> dict[str, str]:
    deployments = [
        d for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "hub-api"
    ]
    assert len(deployments) == 1, f"expected exactly one hub-api Deployment, got {len(deployments)}"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    assert containers, "hub-api Deployment has no containers"
    return {e["name"]: e.get("value", "") for c in containers for e in c.get("env", [])}


def _svc_streaming_service_port(docs: list[dict[str, Any]]) -> tuple[str, int]:
    services = [
        d for d in docs
        if d.get("kind") == "Service"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "svc-streaming"
        and d["spec"].get("type") == "ClusterIP"
    ]
    assert len(services) == 1, f"expected one ClusterIP svc-streaming Service, got {len(services)}"
    http = [p for p in services[0]["spec"]["ports"] if p["name"] == "http"]
    assert len(http) == 1
    return services[0]["metadata"]["name"], http[0]["port"]


def test_hub_api_streaming_url_matches_the_rendered_service() -> None:
    docs = _render([])
    name, port = _svc_streaming_service_port(docs)
    assert _hub_api_env(docs)["STREAMING_RTC_URL"] == f"http://{name}:{port}"
    assert port == 8208


def test_url_follows_a_port_override() -> None:
    docs = _render(["pipeline.svcStreaming.port=9123"])
    assert _hub_api_env(docs)["STREAMING_RTC_URL"].endswith(":9123")


def test_env_absent_when_svc_streaming_disabled() -> None:
    docs = _render(["pipeline.svcStreaming.enabled=false"])
    assert "STREAMING_RTC_URL" not in _hub_api_env(docs)
