"""Helm-render assertions: hub-api's ANALYTICS_CORE_API_URL targets the consolidated core-data Service.

regression: gh-894 -- hub-api's analytics proxy defaulted to the retired ``analytics-core`` host,
so every ``/api/v1/analytics/platform/*`` call failed. ``helm`` missing skips locally but FAILS in CI.
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


def _render(*extra_set: str) -> list[dict[str, Any]]:
    """Render the alpha chart with extra --set values; zero documents is a hard failure."""
    if not shutil.which("helm"):
        if os.getenv("CI"):
            pytest.fail("helm CLI is required in CI")
        pytest.skip("helm CLI not available in this environment")
    argv = [
        "helm", "template", "waddlebot", str(_CHART_DIR),
        "--kube-version", "1.30.0", "--values", str(_ALPHA_VALUES),
    ]  # fmt: skip
    for item in extra_set:
        argv += ["--set", item]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stdout}\n{result.stderr}")
    rendered = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert rendered, "helm template produced zero documents -- cannot be a pass"
    return rendered


def _hub_api_env(docs: list[dict[str, Any]]) -> dict[str, str]:
    """Return the hub-api container's literal env as a name -> value map."""
    deployments = [
        d for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "hub-api"
    ]  # fmt: skip
    assert len(deployments) == 1, f"expected 1 hub-api Deployment, got {len(deployments)}"
    container = deployments[0]["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in container["env"]}


def _core_data_service(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the single core-data Service document."""
    services = [
        d for d in docs
        if d.get("kind") == "Service"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "core-data"
    ]  # fmt: skip
    assert len(services) == 1, f"expected 1 core-data Service, got {len(services)}"
    return services[0]


def test_analytics_url_points_at_core_data_service() -> None:
    """Default env resolves to the rendered core-data Service name + http port."""
    docs = _render()
    svc = _core_data_service(docs)
    http_port = next(p["port"] for p in svc["spec"]["ports"] if p["name"] == "http")
    url = _hub_api_env(docs)["ANALYTICS_CORE_API_URL"]
    assert url.startswith(f"http://{svc['metadata']['name']}."), url
    assert url.endswith(f":{http_port}")
    assert "analytics-core" not in url


def test_analytics_url_follows_release_fullname() -> None:
    """The host is derived from the release fullname, never hardcoded."""
    docs = _render("fullnameOverride=wb-x")
    svc = _core_data_service(docs)
    assert svc["metadata"]["name"] == "wb-x-core-data"
    assert _hub_api_env(docs)["ANALYTICS_CORE_API_URL"].startswith("http://wb-x-core-data.")


def test_analytics_url_explicit_override_wins() -> None:
    """pipeline.hubApi.analyticsCoreUrl overrides the derived value."""
    docs = _render("pipeline.hubApi.analyticsCoreUrl=https://analytics.example.internal:9000")
    assert _hub_api_env(docs)["ANALYTICS_CORE_API_URL"] == "https://analytics.example.internal:9000"


def test_analytics_url_omitted_when_core_data_disabled_and_no_override() -> None:
    """No core-data and no override: env absent (proxy fails loud with 502, never a bogus host)."""
    docs = _render("modules.coreData.enabled=false")
    assert "ANALYTICS_CORE_API_URL" not in _hub_api_env(docs)
