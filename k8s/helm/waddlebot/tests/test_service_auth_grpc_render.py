"""Helm-template assertions for the machine-JWT bootstrap moving to gRPC.

`POST /internal/service-token` and `GET /internal/service-jwks.json` are gone; every caller mints
(`SERVICE_JWT_TOKEN_ENDPOINT`) and fetches the JWKS (`PUSH_JWKS_ENDPOINT`,
`MACHINE_JWT_JWKS_ENDPOINT`) over hub-api's internal gRPC `ServiceAuthService`:

1. every endpoint is `https://<hub-api>:<grpcPort>` -- the gRPC port, TLS, never the REST port/path;
2. every such pod pins the CA that signed hub-api's gRPC cert (`HUB_API_GRPC_CA_FILE` + mount), since
   the client is TLS-1.3-only and trusts exactly that CA (a missing CA is a startup/first-use error);
3. hub-api's gRPC CiliumNetworkPolicy admits every caller, including egress-proxy (JWKS).

Zero matching workloads is a hard failure, never a silent pass (critical-rules.md Verification
Integrity): the denominator is reported in each assertion message.
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
_ENDPOINT_VARS = ("SERVICE_JWT_TOKEN_ENDPOINT", "PUSH_JWKS_ENDPOINT", "MACHINE_JWT_JWKS_ENDPOINT")
#: Components that must reach the mint/JWKS over gRPC in the alpha render.
_EXPECTED_COMPONENT_SUFFIXES = (
    "-svc-ingest-rust",
    "-svc-process-rust",
    "-svc-action-rust",
    "-svc-presentation",
    "-egress-proxy",
)


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    result = subprocess.run(
        [
            "helm", "template", "waddlebot", str(_CHART_DIR),
            "--kube-version", "1.30.0",
            "--values", str(_ALPHA_VALUES),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    rendered = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert rendered, "helm template produced zero documents -- cannot be a pass"
    return rendered


def _bootstrap_deployments(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Deployment name -> its first container, for every pod using a gRPC bootstrap endpoint."""
    found: dict[str, dict[str, Any]] = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            names = {e["name"] for e in container.get("env", [])}
            if names & set(_ENDPOINT_VARS):
                found[doc["metadata"]["name"]] = container
    return found


def _grpc_port(docs: list[dict[str, Any]]) -> str:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith(("-hub-api", "-hub-api-v3")):
            for env in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
                if env["name"] == "GRPC_PORT":
                    return str(env["value"])
    pytest.fail("hub-api Deployment with GRPC_PORT not found")


def test_every_bootstrap_endpoint_is_hub_api_grpc_over_tls(docs: list[dict[str, Any]]) -> None:
    deployments = _bootstrap_deployments(docs)
    for suffix in _EXPECTED_COMPONENT_SUFFIXES:
        assert any(name.endswith(suffix) for name in deployments), (
            f"no {suffix} Deployment uses a gRPC bootstrap endpoint; "
            f"examined {sorted(deployments)}"
        )
    port = _grpc_port(docs)
    checked = 0
    for name, container in deployments.items():
        env = {e["name"]: e.get("value") for e in container["env"]}
        for var in _ENDPOINT_VARS:
            if var not in env:
                continue
            checked += 1
            value = env[var]
            assert value.startswith("https://"), f"{name}.{var}={value!r}: TLS is mandatory"
            assert value.endswith(f"-hub-api-v3:{port}"), (
                f"{name}.{var}={value!r}: must be hub-api's gRPC port {port}, not the REST port"
            )
            assert "/internal/" not in value, f"{name}.{var}={value!r}: REST path left behind"
    assert checked >= len(_EXPECTED_COMPONENT_SUFFIXES), f"only {checked} endpoints examined"


def test_every_bootstrap_caller_pins_the_hub_api_grpc_ca(docs: list[dict[str, Any]]) -> None:
    deployments = _bootstrap_deployments(docs)
    assert deployments, "zero bootstrap callers examined -- cannot be a pass"
    for name, container in deployments.items():
        env = {e["name"]: e.get("value") for e in container["env"]}
        ca_file = env.get("HUB_API_GRPC_CA_FILE")
        assert ca_file, f"{name}: HUB_API_GRPC_CA_FILE missing -- the TLS 1.3 client has no CA"
        mounts = [m["mountPath"] for m in container.get("volumeMounts", [])]
        assert any(ca_file.startswith(m.rstrip("/") + "/") for m in mounts), (
            f"{name}: {ca_file} is not under a volumeMount ({mounts})"
        )


def test_hub_api_grpc_policy_admits_every_bootstrap_caller(docs: list[dict[str, Any]]) -> None:
    policies = [
        d for d in docs
        if d.get("kind") == "CiliumNetworkPolicy" and d["metadata"]["name"].endswith("-hub-api-grpc")
    ]
    assert len(policies) == 1, f"expected exactly one hub-api gRPC policy, found {len(policies)}"
    allowed: set[str] = set()
    for rule in policies[0]["spec"]["ingress"]:
        for peer in rule["fromEndpoints"]:
            for expr in peer["matchExpressions"]:
                allowed.update(expr["values"])
    for component in ("svc-ingest-rust", "svc-process-rust", "svc-action-rust", "svc-presentation", "egress-proxy"):
        assert component in allowed, f"{component} cannot reach hub-api's gRPC port: {sorted(allowed)}"
