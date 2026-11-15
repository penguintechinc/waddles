"""Helm-template assertions for the interactions/webhook Gateway route (command-latency first door).

`interactionsGateway` renders, only when enabled, an HTTPRoute for svc-ingest's HTTP intake
(`POST /intake/webhook/*`) on the shared Gateway whose RequestHeaderModifier SETS `x-request-start`
(overwrite, never append), a CiliumNetworkPolicy admitting the Gateway to svc-ingest's HTTP port ONLY,
and `INGEST_TRUST_EDGE_REQUEST_START` on svc-ingest. Proven across EVERY environment values file and
BOTH helm majors (v3.17.3 at ~/.local/bin/helm3 and the default v4); a missing helm is a skip locally
and a FAILURE in CI. Zero rendered documents or zero examined files is a failure, never a pass.
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
_VALUES = sorted(p.name for p in _CHART_DIR.glob("values-*.yaml"))
_HELM3 = Path.home() / ".local" / "bin" / "helm3"
_HELMS = {
    "helm3": str(_HELM3) if _HELM3.exists() else None,
    "helm4": shutil.which("helm"),
}
# dal2alpha enables the route in its own values; every other environment needs the prerequisites.
_ENABLE = [
    "interactionsGateway.enabled=true",
    "httproute.enabled=true",
    "httproute.hostnames={app.example.test}",
    "pipeline.rustDataPlane.enabled=true",
]
# Prerequisites other environments' own guards demand just to render (unrelated to this feature).
_BASE = [
    "infrastructure.seaweedfs.encryption.secretName=seaweedfs-sse-kek",
    "pipeline.rustDataPlane.egressClusterCidrDenylist={10.0.0.0/8}",
    "global.hostApiTls.stageIdentity=spiffe://example.test/host-api",
]
_ROUTE_SUFFIX = "-interactions"
_CNP_SUFFIX = "-svc-ingest-interactions-gateway"


def _render(
    helm: str, values: str | None, *sets: str, allow_fail: bool = False
) -> list[dict[str, Any]] | subprocess.CompletedProcess[str]:
    """`helm template` the chart; returns documents (or the failed process when `allow_fail`)."""
    argv = [helm, "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0"]
    if values:
        argv += ["--values", str(_CHART_DIR / values)]
    for item in (*_BASE, *sets):
        argv += ["--set", item]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only
    if allow_fail:
        return result
    if result.returncode != 0:
        pytest.fail(f"helm template failed ({values}):\n{result.stdout}\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, "helm template produced zero documents -- cannot be a pass"
    return docs


@pytest.fixture(params=sorted(_HELMS))
def helm(request: pytest.FixtureRequest) -> str:
    """Each helm major in turn; skip locally (fail in CI) when it is not installed."""
    path = _HELMS[request.param]
    if not path:
        if os.getenv("CI"):
            pytest.fail(f"{request.param} is required in CI")
        pytest.skip(f"{request.param} not available in this environment")
    return path


def _by(docs: list[dict[str, Any]], kind: str, suffix: str) -> list[dict[str, Any]]:
    """Documents of `kind` whose name ends with `suffix`."""
    return [
        d for d in docs if d.get("kind") == kind and d["metadata"]["name"].endswith(suffix)
    ]


def test_there_are_environment_values_files() -> None:
    """Denominator guard: the parametrized env sweep must examine at least one file."""
    assert "values-dal2alpha.yaml" in _VALUES and len(_VALUES) >= 6


@pytest.mark.parametrize("values", _VALUES)
def test_off_by_default_in_every_env_except_dal2alpha(helm: str, values: str) -> None:
    """No route, no CNP and no trust env unless the environment enables it (alpha does)."""
    docs = _render(helm, values)
    routes = _by(docs, "HTTPRoute", _ROUTE_SUFFIX)
    cnps = _by(docs, "CiliumNetworkPolicy", _CNP_SUFFIX)
    expect = 1 if values == "values-dal2alpha.yaml" else 0
    assert (len(routes), len(cnps)) == (expect, expect), values


@pytest.mark.parametrize("values", _VALUES)
def test_enabled_renders_route_cnp_and_trust_env(helm: str, values: str) -> None:
    """Enabled in any env: stamping route (SET), HTTP-port-only CNP, svc-ingest trusts the header."""
    docs = _render(helm, values, *_ENABLE)
    [route] = _by(docs, "HTTPRoute", _ROUTE_SUFFIX)
    rule = route["spec"]["rules"][0]
    assert rule["matches"][0]["path"] == {"type": "PathPrefix", "value": "/intake/webhook"}
    assert rule["matches"][0]["method"] == "POST"
    [flt] = rule["filters"]
    assert flt["type"] == "RequestHeaderModifier"
    modifier = flt["requestHeaderModifier"]
    assert "add" not in modifier, "must SET (overwrite), never append"
    assert modifier["set"] == [
        {"name": "x-request-start", "value": "t=%START_TIME(%s.%3f)%"}
    ]
    backend = rule["backendRefs"][0]
    assert backend["name"].endswith("-svc-ingest-rust") and backend["port"] == 8200

    [cnp] = _by(docs, "CiliumNetworkPolicy", _CNP_SUFFIX)
    assert cnp["spec"]["endpointSelector"]["matchLabels"][
        "app.kubernetes.io/component"
    ] == "svc-ingest-rust"
    ports = {
        (p["port"], p["protocol"])
        for rule_ in cnp["spec"]["ingress"]
        for tp in rule_["toPorts"]
        for p in tp["ports"]
    }
    assert ports == {("8200", "TCP")}, "Gateway -> svc-ingest on the HTTP port ONLY"

    [ingest] = _by(docs, "Deployment", "-svc-ingest-rust")
    env = {
        e["name"]: e.get("value")
        for e in ingest["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["INGEST_TRUST_EDGE_REQUEST_START"] == "true"


def test_without_edge_stamp_there_is_no_filter_and_no_trust(helm: str) -> None:
    """edgeRequestStart.enabled=false: route only, no header filter, svc-ingest does not trust it."""
    docs = _render(
        helm, "values-dal2alpha.yaml", "interactionsGateway.edgeRequestStart.enabled=false"
    )
    [route] = _by(docs, "HTTPRoute", _ROUTE_SUFFIX)
    assert "filters" not in route["spec"]["rules"][0]
    [ingest] = _by(docs, "Deployment", "-svc-ingest-rust")
    names = [e["name"] for e in ingest["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert "INGEST_TRUST_EDGE_REQUEST_START" not in names


def test_enabled_without_a_gateway_fails_loudly(helm: str) -> None:
    """No httproute.enabled = no Gateway to attach to: a render error, never a silent skip."""
    result = _render(
        helm,
        "values-alpha.yaml",
        "interactionsGateway.enabled=true",
        "pipeline.rustDataPlane.enabled=true",
        allow_fail=True,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0
    assert "requires httproute.enabled" in (result.stderr + result.stdout)
