"""Helm-template assertions for bundleExecutor.runtimeClass (optional Kata isolation).

Off by default (no runtimeClassName anywhere); on renders it on exactly the two executor
Deployments and no other workload; enabled with an empty name fails the render. Each test
asserts the number of executor Deployments examined (zero = failure, never a pass).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_EXECUTORS = ("-bundle-executor", "-bundle-executor-action")
_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
_ENVS = ["values-alpha.yaml", "values-dal2alpha.yaml"]


def _render(values: str, sets: list[str]) -> subprocess.CompletedProcess[str]:
    """Run helm template for a values file plus --set overrides."""
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
            "--values", str(_CHART_DIR / values)]
    for s in sets:
        flag = "--set-string" if "nodeSelector" in s else "--set"
        args += [flag, s]
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only


def _docs(values: str, sets: list[str]) -> list[dict[str, Any]]:
    """Render and parse; helm failure or zero documents fails the test."""
    res = _render(values, sets)
    if res.returncode != 0:
        pytest.fail(f"helm template failed:\n{res.stderr}")
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs
    return docs


def _pod(doc: dict[str, Any]) -> dict[str, Any]:
    """Pod spec of a workload doc."""
    if doc["kind"] == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return doc["spec"]["template"]["spec"]


def _split(docs: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Return (executor workloads, all other workloads)."""
    ex, other = [], []
    for d in docs:
        if d.get("kind") not in _KINDS:
            continue
        (ex if d["metadata"]["name"].endswith(_EXECUTORS) else other).append(d)
    return ex, other


@pytest.fixture(scope="module", autouse=True)
def _require_helm() -> None:
    if not shutil.which("helm"):
        pytest.skip("helm CLI not available in this environment")


@pytest.mark.parametrize("values", _ENVS)
def test_off_by_default(values: str) -> None:
    ex, other = _split(_docs(values, []))
    assert len(ex) == 2, f"expected 2 executor Deployments, examined {len(ex)}"
    for d in ex + other:
        assert "runtimeClassName" not in _pod(d) or d["metadata"]["name"].endswith("streaming")


@pytest.mark.parametrize("values", _ENVS)
def test_on_renders_only_on_executors(values: str) -> None:
    sets = ["bundleExecutor.runtimeClass.enabled=true",
            "bundleExecutor.nodeSelector.katacontainers\\.io/kata-runtime=true"]
    ex, other = _split(_docs(values, sets))
    assert len(ex) == 2, f"expected 2 executor Deployments, examined {len(ex)}"
    for d in ex:
        pod = _pod(d)
        assert pod["runtimeClassName"] == "kata"
        assert pod["nodeSelector"] == {"katacontainers.io/kata-runtime": "true"}
        assert pod["containers"][0]["resources"]["requests"]["memory"] == "512Mi"
    assert other, "no non-executor workloads examined"
    for d in other:
        assert _pod(d).get("runtimeClassName") != "kata", d["metadata"]["name"]


def test_enabled_with_empty_name_fails_loud() -> None:
    res = _render("values-alpha.yaml", ["bundleExecutor.runtimeClass.enabled=true",
                                        "bundleExecutor.runtimeClass.name="])
    assert res.returncode != 0
    assert "runtimeClass.name" in res.stderr
