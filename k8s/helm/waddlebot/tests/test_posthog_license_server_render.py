"""Helm render tests: flags come from the license-server-hosted PostHog (hub-api + Rust data plane).

dal2alpha must point POSTHOG_HOST at https://license.penguintech.io, wire the project key from an
operator Secret via an OPTIONAL secretKeyRef (missing Secret never crashes the pod), keep the
FLAG_* env baselines, and leave hub-api egress able to reach the license server on 443. Zero
matching resources is a hard failure, never a silent pass.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ALPHA = ["values-alpha.yaml", "values-dal2alpha.yaml"]
_SECRET = "waddlebot-posthog-license"
_REF = {"name": _SECRET, "key": "POSTHOG_API_KEY", "optional": True}


def _render(values: list[str], *sets: str) -> list[dict[str, Any]]:
    """Render the chart with the given values files / --set args; return all documents."""
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    cmd = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0"]
    for v in values:
        cmd += ["--values", str(_CHART_DIR / v)]
    for s in sets:
        cmd += ["--set", s]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if res.returncode != 0:
        pytest.fail(f"helm template failed:\n{res.stderr}")
    return [d for d in yaml.safe_load_all(res.stdout) if d]


def _containers(docs: list[dict[str, Any]], dep: str, cname: str) -> list[dict[str, Any]]:
    """Containers named `cname` in Deployments whose name contains `dep`."""
    return [
        c
        for d in docs
        if d.get("kind") == "Deployment" and dep in d["metadata"]["name"]
        for c in d["spec"]["template"]["spec"]["containers"]
        if c["name"] == cname
    ]


def _key_ref(container: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The secretKeyRef of env var `name` on `container`, or None if absent."""
    hits = [e for e in container.get("env", []) if e["name"] == name]
    assert len(hits) <= 1, f"duplicate env {name}"
    return hits[0]["valueFrom"]["secretKeyRef"] if hits else None


def _config_map(docs: list[dict[str, Any]]) -> dict[str, str]:
    """The single shared config ConfigMap data (the one carrying POSTHOG_HOST)."""
    cms = [d for d in docs if d["kind"] == "ConfigMap" and "POSTHOG_HOST" in d.get("data", {})]
    assert len(cms) == 1, f"expected 1 config ConfigMap, got {len(cms)}"
    return cms[0]["data"]


def test_hub_api_key_is_optional_secret_key_ref() -> None:
    """dal2alpha hub-api reads POSTHOG_API_KEY from the operator Secret, optional."""
    found = _containers(_render(_ALPHA), "hub-api", "hub-api")
    assert len(found) == 1, f"expected 1 hub-api container, found {len(found)}"
    assert _key_ref(found[0], "POSTHOG_API_KEY") == _REF


def test_hub_api_keeps_flag_baselines() -> None:
    """The #1462 FLAG_* baselines remain alongside the PostHog wiring."""
    found = _containers(_render(_ALPHA), "hub-api", "hub-api")
    assert len(found) == 1
    assert [e["name"] for e in found[0]["env"] if e["name"].startswith("FLAG_")]


def test_posthog_host_is_license_server() -> None:
    """Shared config points POSTHOG_HOST at the license server (in-cluster PostHog is off)."""
    assert _config_map(_render(_ALPHA))["POSTHOG_HOST"] == "https://license.penguintech.io"


def test_host_comes_from_values() -> None:
    """posthog.licenseServer.host drives POSTHOG_HOST when posthog.host is blank."""
    docs = _render(_ALPHA, "posthog.licenseServer.host=https://flags.example.test")
    assert _config_map(docs)["POSTHOG_HOST"] == "https://flags.example.test"


@pytest.mark.parametrize("dep", ["svc-process-rust", "svc-action-rust"])
def test_rust_services_use_same_secret(dep: str) -> None:
    """The Rust data plane reads POSTHOG_KEY from the same optional Secret."""
    found = _containers(_render(_ALPHA), dep, dep)
    assert len(found) == 1, f"expected 1 {dep} container, found {len(found)}"
    assert _key_ref(found[0], "POSTHOG_KEY") == _REF


def test_chart_default_renders_no_key_ref() -> None:
    """With existingSecret blank no POSTHOG_API_KEY env is rendered."""
    found = _containers(_render(_ALPHA, "posthog.licenseServer.existingSecret="), "hub-api", "hub-api")
    assert len(found) == 1
    assert _key_ref(found[0], "POSTHOG_API_KEY") is None


def test_hub_api_egress_allows_license_server_443() -> None:
    """hub-api's CNP allows external 443 (no FQDN pin that would exclude license.penguintech.io)."""
    cnps = [
        d
        for d in _render(_ALPHA)
        if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"].endswith("-hub-api-policy")
    ]
    assert len(cnps) == 1, f"expected 1 hub-api CNP, got {len(cnps)}"
    https = [
        r
        for r in cnps[0]["spec"]["egress"]
        if ("toCIDRSet" in r or "toFQDNs" in r)
        and {"port": "443", "protocol": "TCP"} in r["toPorts"][0]["ports"]
    ]
    assert len(https) == 1, f"expected 1 external-443 rule, got {len(https)}"
    if "toFQDNs" in https[0]:
        assert {"matchName": "license.penguintech.io"} in https[0]["toFQDNs"]
