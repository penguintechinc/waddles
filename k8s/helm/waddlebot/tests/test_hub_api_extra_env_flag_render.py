"""Helm-template assertions for the hub-api FLAG_* env baseline (pipeline.hubApi.extraEnv).

dal2alpha (values-alpha.yaml + values-dal2alpha.yaml, as deploy-dal2alpha.yml layers them) must render
FLAG_WADDLES_TENANT_DISCORD_INSTALL=true on the hub-api container; chart defaults must not. Zero
hub-api containers found is a hard failure, never a silent pass.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_FLAG = "FLAG_WADDLES_TENANT_DISCORD_INSTALL"


def _hub_api_env(values: list[str]) -> dict[str, Any]:
    """Render the chart with the given values files and return the hub-api container env map."""
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")
    cmd = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0"]
    for v in values:
        cmd += ["--values", str(_CHART_DIR / v)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        pytest.fail(f"helm template failed:\n{result.stderr}")
    found = []
    for doc in yaml.safe_load_all(result.stdout):
        if not doc or doc.get("kind") != "Deployment" or "hub-api" not in doc["metadata"]["name"]:
            continue
        for c in doc["spec"]["template"]["spec"]["containers"]:
            if c["name"] == "hub-api":
                found.append({e["name"]: e.get("value") for e in c.get("env", [])})
    assert len(found) == 1, f"expected exactly 1 hub-api container, found {len(found)}"
    return found[0]


def test_dal2alpha_sets_tenant_discord_install_flag() -> None:
    """dal2alpha overlay enables the free-tier tenant Discord install flag baseline."""
    env = _hub_api_env(["values-alpha.yaml", "values-dal2alpha.yaml"])
    assert env.get(_FLAG) == "true"


def test_default_values_omit_flag() -> None:
    """Chart-default extraEnv ({}) never sets the flag baseline.

    Bare values.yaml cannot render standalone (production tier refuses to auto-generate TLS), so this
    renders values-alpha.yaml alone, which does not override pipeline.hubApi.extraEnv.
    """
    env = _hub_api_env(["values-alpha.yaml"])
    assert _FLAG not in env
