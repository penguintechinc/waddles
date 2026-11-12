"""hub-api's SPIFFE_ENV comes from `spire.environment`; empty is a render failure.

The stream DEK broker pins caller SPIFFE ids to this env and fails closed when it is unset, so
the chart must always set it. Zero hub-api Deployments is a hard failure, never a silent pass.
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


def _helm(values: str, extra_set: list[str]) -> subprocess.CompletedProcess[str]:
    """Run `helm template` against one values file with optional --set overrides."""
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
            "--values", str(_CHART_DIR / values)]
    for kv in extra_set:
        args += ["--set", kv]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _hub_api_env(stdout: str) -> dict[str, str]:
    """Env map of the single rendered hub-api Deployment."""
    docs: list[dict[str, Any]] = [d for d in yaml.safe_load_all(stdout) if d]
    deployments = [
        d for d in docs
        if d.get("kind") == "Deployment"
        and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "hub-api"
    ]
    assert len(deployments) == 1, f"expected exactly one hub-api Deployment, got {len(deployments)}"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value", "") for c in containers for e in c.get("env", [])}


def test_dal2alpha_sets_spiffe_env_alpha() -> None:
    result = _helm("values-dal2alpha.yaml", [])
    assert result.returncode == 0, result.stderr
    assert _hub_api_env(result.stdout)["SPIFFE_ENV"] == "alpha"


def test_spiffe_env_follows_spire_environment() -> None:
    result = _helm("values-dal2alpha.yaml", ["spire.environment=beta"])
    assert result.returncode == 0, result.stderr
    assert _hub_api_env(result.stdout)["SPIFFE_ENV"] == "beta"


def test_empty_spire_environment_fails_render() -> None:
    result = _helm("values-dal2alpha.yaml", ["spire.environment="])
    assert result.returncode != 0
    assert "spire.environment is required" in result.stderr
