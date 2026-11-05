"""Helm-template assertions for the P6a `waddles.connector-tools` boot wiring.

Flag OFF (the default, incl. values-alpha.yaml) must render NONE of the executor
wiring; ON must render the env/port/volumes svc_ingest validates at boot; a
half-configured ON must fail `helm template` (never render a pod that crash-loops).
Zero rendered documents or zero matching Deployments is a hard failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_DIGEST = "sha256:" + "a" * 64
_ON = [
    "--set", "pipeline.rustDataPlane.svcIngest.connectorTools.enabled=true",
    "--set", f"pipeline.rustDataPlane.svcIngest.connectorTools.digests.twitch={_DIGEST}",
    "--set", f"pipeline.rustDataPlane.svcIngest.connectorTools.digests.discord={_DIGEST}",
    "--set", f"pipeline.rustDataPlane.svcIngest.connectorTools.digests.youtube={_DIGEST}",
]


def _render(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, test-only
        ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
         "--values", str(_CHART_DIR / "values-alpha.yaml"), *extra],
        capture_output=True, text=True, check=False,
    )


def _ingest(extra: list[str]) -> dict[str, Any]:
    res = _render(*extra)
    assert res.returncode == 0, res.stderr[-500:]
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "helm rendered zero documents"
    found = [d for d in docs if d.get("kind") == "Deployment"
             and d["metadata"]["name"].endswith("-svc-ingest-rust")]
    assert len(found) == 1, f"expected exactly one svc-ingest-rust Deployment, got {len(found)}"
    return found[0]


def _env(dep: dict[str, Any]) -> dict[str, Any]:
    c = dep["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in c["env"]}


def test_flag_off_renders_no_executor_wiring() -> None:
    dep = _ingest([])
    env = _env(dep)
    for name in ("CONNECTOR_TOOLS_ENABLED", "SENDER_EXEC_DIGEST_TWITCH", "TWITCH_CLIENT_ID"):
        assert name not in env, name
    ports = [p["name"] for p in dep["spec"]["template"]["spec"]["containers"][0]["ports"]]
    assert "host-api" not in ports


def test_flag_on_renders_env_port_and_tls_volumes() -> None:
    dep = _ingest(_ON)
    env = _env(dep)
    assert env["CONNECTOR_TOOLS_ENABLED"]["value"] == "true"
    for p in ("TWITCH", "DISCORD", "YOUTUBE"):
        assert env[f"SENDER_EXEC_DIGEST_{p}"]["value"] == _DIGEST
    assert "secretKeyRef" in env["TWITCH_CLIENT_ID"]["valueFrom"]
    for name in ("HOST_API_SERVER_CERT_FILE", "HOST_API_SERVER_KEY_FILE", "HOST_API_CLIENT_CA_FILE",
                 "HUB_API_GRPC_ENDPOINT", "HUB_API_GRPC_CA_FILE", "OUTBOUND_PIN_KEYS",
                 "OUTBOUND_PIN_ACTIVE_KID", "SERVICE_JWT_TOKEN_ENDPOINT"):
        assert name in env, name
    assert "value" not in env["OUTBOUND_PIN_KEYS"], "pin keys must come from a Secret"
    spec = dep["spec"]["template"]["spec"]
    assert "host-api" in [p["name"] for p in spec["containers"][0]["ports"]]
    vols = {v["name"] for v in spec["volumes"]}
    assert {"host-api-tls", "hub-api-grpc-ca"} <= vols


@pytest.mark.parametrize(
    "bad",
    [
        ["--set", "pipeline.rustDataPlane.svcIngest.connectorTools.enabled=true"],
        [*_ON[:2], "--set", "pipeline.rustDataPlane.svcIngest.connectorTools.digests.twitch=latest"],
        [*_ON, "--set", "pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled=false"],
    ],
)
def test_half_configured_on_fails_render(bad: list[str]) -> None:
    res = _render(*bad)
    assert res.returncode != 0
    assert "connectorTools" in res.stderr
