"""Helm render assertions for the landing-stats refresh CronJob."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_VALUES = _CHART_DIR / "values-alpha.yaml"


def _render(extra: list[str] | None = None) -> list[dict[str, Any]]:
    helm = shutil.which("helm") or shutil.which("helm3")
    if not helm:
        pytest.skip("helm CLI not available")
    args = [
        helm,
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_VALUES),
    ]
    for kv in extra or []:
        args += ["--set", kv]
    res = subprocess.run(args, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs
    return docs


def _cron(docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for d in docs:
        if d.get("kind") == "CronJob" and d["metadata"]["name"].endswith(
            "-landing-stats-refresh"
        ):
            return d
    return None


def test_cronjob_rendered_by_default() -> None:
    cron = _cron(_render())
    assert cron is not None
    spec = cron["spec"]
    assert spec["schedule"] == "*/5 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    c = pod["containers"][0]
    assert c["command"] == ["python3", "-m", "cli.refresh_landing_stats"]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    names = {e["name"] for e in c["env"]}
    assert {"VALKEY_URL", "VALKEY_PASSWORD"} <= names


def test_cronjob_disabled_by_knob() -> None:
    assert _cron(_render(["pipeline.hubApi.landingStatsRefresh.enabled=false"])) is None
