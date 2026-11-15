"""hub-api gets its svc-streaming gRPC target from Helm, matching the Service it actually talks to.

Regression: `hub_api/services/calls_proxy.py` once defaulted to `http://svc-streaming:8093` while the
chart serves svc-streaming under `<release>-svc-streaming`. Since the RoomsService port
(`STREAMING_RTC_GRPC_TARGET`, gRPC over mTLS) replaced the REST proxy, the target must be the
Service's `grpc` port. Zero matching Deployments/containers is a hard failure, never a silent pass.
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
    grpc_ports = [p for p in services[0]["spec"]["ports"] if p["name"] == "grpc"]
    assert len(grpc_ports) == 1
    return services[0]["metadata"]["name"], grpc_ports[0]["port"]


def test_hub_api_streaming_target_matches_the_rendered_service() -> None:
    docs = _render([])
    name, port = _svc_streaming_service_port(docs)
    env = _hub_api_env(docs)
    assert env["STREAMING_RTC_GRPC_TARGET"] == f"{name}:{port}"
    assert port == 50208
    assert "STREAMING_RTC_URL" not in env, "the REST proxy env is retired"


def test_target_follows_a_grpc_port_override() -> None:
    docs = _render(["pipeline.svcStreaming.grpcPort=51234"])
    assert _hub_api_env(docs)["STREAMING_RTC_GRPC_TARGET"].endswith(":51234")


def test_env_absent_when_svc_streaming_disabled() -> None:
    docs = _render(["pipeline.svcStreaming.enabled=false"])
    assert "STREAMING_RTC_GRPC_TARGET" not in _hub_api_env(docs)
