"""Helm render assertions for the X-Forwarded-Host license-bypass trust wiring.

# regression: alpha hub-api never saw the public host, so free-tier caps applied

Zero rendered documents is a hard failure, never a silent pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_VALUES = _CHART_DIR / "values-dal2alpha.yaml"


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    """helm template with values-dal2alpha.yaml plus --set overrides."""
    argv = ["helm", "template", "waddlebot", str(_CHART_DIR), "-f", str(_VALUES), "--kube-version", "1.31.0"]
    for item in sets:
        argv += ["--set", item]
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603


def _docs(out: str) -> list[dict[str, Any]]:
    """Parse rendered manifests; zero documents is a failure."""
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert docs, "helm rendered zero documents"
    return docs


def _env(docs: list[dict[str, Any]], name: str) -> dict[str, str]:
    """Env name->value map of the first container of Deployment `name`."""
    for d in docs:
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == name:
            return {
                e["name"]: e.get("value", "")
                for e in d["spec"]["template"]["spec"]["containers"][0]["env"]
            }
    raise AssertionError(f"deployment {name} not rendered")


def test_dal2alpha_enables_trust_and_allowlist() -> None:
    """dal2alpha turns trust on, sets CIDRs and the hostname allowlist."""
    res = _render()
    assert res.returncode == 0, res.stderr
    docs = _docs(res.stdout)
    api = _env(docs, "waddlebot-hub-api-v3")
    assert api["TRUSTED_FORWARDED_HOST"] == "1"
    assert api["TRUSTED_PROXY_CIDRS"] == "10.244.0.0/16"
    web = _env(docs, "waddlebot-hub-webui-v3")
    assert web["PUBLIC_HOSTNAMES"] == "waddles-alpha.penguintech.cloud"


def test_flag_off_renders_no_trust_env() -> None:
    """With the flag off no trust env reaches hub-api."""
    res = _render("hubApi.trustForwardedHost=false")
    assert res.returncode == 0, res.stderr
    assert "TRUSTED_FORWARDED_HOST" not in _env(_docs(res.stdout), "waddlebot-hub-api-v3")


def test_render_fails_when_route_targets_hub_api() -> None:
    """Trust + a route that fronts hub-api directly must fail the render."""
    res = _render("httproute.rules[0].service=waddlebot-hub-api-v3")
    assert res.returncode != 0
    assert "only hub-webui may front it" in res.stderr


def test_render_fails_without_trusted_cidrs() -> None:
    """Trust without trustedProxyCidrs must fail the render."""
    res = _render("hubApi.trustedProxyCidrs=null")
    assert res.returncode != 0
    assert "trustedProxyCidrs" in res.stderr
