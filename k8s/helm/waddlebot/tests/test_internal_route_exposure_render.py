"""Helm render assertions: hub-api `/api/v1/internal/*` is never publicly routable.

# regression: internal machine-JWT routes reachable from the internet via the public proxy path

The chart must refuse to render an Ingress path or HTTPRoute rule that names the internal prefix,
or that sends a covering prefix (`/`, `/api`, `/api/v1`) directly to hub-api, while the shipped
configuration (hub-webui is the only front door) still renders. Zero documents is a failure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_VALUES = _CHART_DIR / "values-dal2alpha.yaml"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm CLI not available")


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    """helm template with values-dal2alpha.yaml plus --set overrides."""
    argv = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "-f", str(_VALUES), "--kube-version", "1.31.0",
    ]
    for item in sets:
        argv += ["--set", item]
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603


def _routes(out: str) -> list[dict[str, Any]]:
    """Parse rendered manifests and return the HTTPRoute documents (>=1 required)."""
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert docs, "helm rendered zero documents"
    routes = [d for d in docs if d.get("kind") == "HTTPRoute"]
    assert routes, "no HTTPRoute rendered -- the test would be vacuous"
    return routes


def test_shipped_dal2alpha_routes_only_to_hub_webui() -> None:
    """No rendered HTTPRoute path touches /api/v1/internal or targets hub-api."""
    res = _render()
    assert res.returncode == 0, res.stderr
    seen = 0
    for route in _routes(res.stdout):
        for rule in route["spec"]["rules"]:
            for match in rule["matches"]:
                assert not match["path"]["value"].lower().startswith("/api/v1/internal")
            for ref in rule["backendRefs"]:
                assert "hub-api" not in ref["name"], ref["name"]
            seen += 1
    assert seen > 0


@pytest.mark.parametrize(
    "path", ["/api/v1/internal", "/api/v1/internal/communities", "/API/V1/Internal/", "/api/v2/internal/x", "/api/v10/internal"]
)
def test_httproute_rule_naming_internal_prefix_fails(path: str) -> None:
    """Any HTTPRoute rule under the internal prefix fails the render."""
    res = _render(f"httproute.rules[0].path={path}")
    assert res.returncode != 0
    assert "exposes hub-api internal routes" in res.stderr


@pytest.mark.parametrize("path", ["/", "/api", "/api/v1", "/api/v1/", "/api/v2"])
def test_httproute_covering_prefix_to_hub_api_fails(path: str) -> None:
    """A covering prefix sent straight to hub-api fails the render."""
    res = _render(
        f"httproute.rules[0].path={path}", "httproute.rules[0].service=waddlebot-hub-api-v3",
        "hubApi.trustForwardedHost=false",
    )
    assert res.returncode != 0
    assert "covering /api/v1/internal directly to hub-api" in res.stderr


def test_httproute_specific_hub_api_prefix_still_renders() -> None:
    """A narrow public hub-api prefix (e.g. /api/v1/auth) is unaffected."""
    res = _render(
        "httproute.rules[0].path=/api/v1/auth", "httproute.rules[0].service=waddlebot-hub-api-v3",
        "httproute.rules[0].port=8204", "hubApi.trustForwardedHost=false",
    )
    assert res.returncode == 0, res.stderr
    assert _routes(res.stdout)


def test_ingress_path_naming_internal_prefix_fails() -> None:
    """An Ingress path under the internal prefix fails the render."""
    res = _render(
        "ingress.enabled=true",
        "ingress.hosts[0].host=example.test",
        "ingress.hosts[0].paths[0].path=/api/v1/internal",
        "ingress.hosts[0].paths[0].pathType=Prefix",
        "ingress.hosts[0].paths[0].service=hub-webui-v3",
        "ingress.hosts[0].paths[0].port=8080",
    )
    assert res.returncode != 0
    assert "exposes hub-api internal routes" in res.stderr


def test_ingress_covering_prefix_to_hub_api_fails() -> None:
    """An Ingress `/api` prefix aimed at hub-api fails the render."""
    res = _render(
        "ingress.enabled=true",
        "hubApi.trustForwardedHost=false",
        "ingress.hosts[0].host=example.test",
        "ingress.hosts[0].paths[0].path=/api",
        "ingress.hosts[0].paths[0].pathType=Prefix",
        "ingress.hosts[0].paths[0].service=hub-api-v3",
        "ingress.hosts[0].paths[0].port=8204",
    )
    assert res.returncode != 0
    assert "covering /api/v1/internal directly to hub-api" in res.stderr


def test_ingress_to_hub_webui_root_still_renders() -> None:
    """The documented shape (`/` -> hub-webui) renders."""
    res = _render(
        "ingress.enabled=true",
        "ingress.hosts[0].host=example.test",
        "ingress.hosts[0].paths[0].path=/",
        "ingress.hosts[0].paths[0].pathType=Prefix",
        "ingress.hosts[0].paths[0].service=hub-webui-v3",
        "ingress.hosts[0].paths[0].port=8080",
    )
    assert res.returncode == 0, res.stderr
    kinds = [d.get("kind") for d in yaml.safe_load_all(res.stdout) if d]
    assert "Ingress" in kinds


@pytest.mark.parametrize("env", ["alpha", "beta", "gamma", "production", "local", "dal2alpha"])
def test_every_environment_values_file_renders_with_public_routes_on(env: str) -> None:
    """# regression: values-beta.yaml routed /api/v1/internal straight to hub-api.

    With the HTTPRoute and Ingress forced on, each shipped values file must pass the render-time
    guard (it fails on any path under /api/v{N}/internal or a covering prefix to hub-api).
    """
    argv = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "-f", str(_CHART_DIR / f"values-{env}.yaml"), "--kube-version", "1.31.0",
        "--set", "httproute.enabled=true", "--set", "ingress.enabled=true",
    ]
    res = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    assert res.returncode == 0, res.stderr
    kinds = {d.get("kind") for d in yaml.safe_load_all(res.stdout) if d}
    assert {"HTTPRoute", "Ingress"} <= kinds
