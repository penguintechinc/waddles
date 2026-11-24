"""Helm-template assertions for bundleExecutor.runtimeClass (optional Kata isolation).

Off by default in EVERY values file (no runtimeClassName anywhere); on renders it on every
svc-bundles-executor StatefulSet pool and no other workload; Kata pods omit the executor
Tetragon TracingPolicy and keep the CiliumNetworkPolicy; the Kata resources override carries its
own LRU budget which must stay below its memory limit; enabled with an empty name fails the
render. Each test asserts the number of executor workloads examined (zero = failure, never a pass).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_EXECUTORS = ("-svc-bundles-executor",)
_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
_ENVS = ["values-alpha.yaml", "values-dal2alpha.yaml"]


def _render(values: str, sets: list[str]) -> subprocess.CompletedProcess[str]:
    """Run helm template for a values file plus --set overrides."""
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
            "--values", str(_CHART_DIR / values),
            # Baseline = the single default pool; the shipped env pools are covered by
            # test_executor_language_partition_render.py.
            "--set-json", "pipeline.rustDataPlane.bundleExecutor.pools=[]"]
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


def _kinds(docs: list[dict[str, Any]], kind: str, needle: str = "svc-bundles-executor") -> list[dict[str, Any]]:
    """Documents of `kind` whose name contains `needle`."""
    return [d for d in docs if d.get("kind") == kind and needle in d["metadata"]["name"]]


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
    assert len(ex) == 1, f"expected 1 executor StatefulSet, examined {len(ex)}"
    for d in ex + other:
        assert "runtimeClassName" not in _pod(d) or d["metadata"]["name"].endswith("streaming")


@pytest.mark.parametrize("values", _ENVS)
def test_on_renders_only_on_executors(values: str) -> None:
    sets = ["bundleExecutor.runtimeClass.enabled=true",
            "bundleExecutor.nodeSelector.katacontainers\\.io/kata-runtime=true"]
    ex, other = _split(_docs(values, sets))
    assert len(ex) == 1, f"expected 1 executor StatefulSet, examined {len(ex)}"
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


@pytest.mark.parametrize("values", ["values.yaml", "values-alpha.yaml", "values-dal2alpha.yaml", "values-beta.yaml",
                                    "values-gamma.yaml", "values-production.yaml", "values-ci.yaml", "values-local.yaml"])
def test_kata_never_enabled_by_any_values_file(values: str) -> None:
    """Kata is opt-in: no shipped values file turns it on."""
    doc = yaml.safe_load((_CHART_DIR / values).read_text())
    rc = ((doc.get("bundleExecutor") or {}).get("runtimeClass")) or {}
    assert rc.get("enabled", False) is False, values
    if values == "values.yaml":
        assert rc["enabled"] is False and rc["name"] == "kata"


@pytest.mark.parametrize("values", _ENVS)
def test_kata_omits_tetragon_policy_and_keeps_cnp(values: str) -> None:
    off = _docs(values, [])
    on = _docs(values, ["bundleExecutor.runtimeClass.enabled=true"])
    # values-alpha.yaml ships tetragon.enabled=false (local alpha), dal2alpha has it on.
    off_tp = {"values-alpha.yaml": 0, "values-dal2alpha.yaml": 1}[values]
    for label, docs, want_tp in (("off", off, off_tp), ("on", on, 0)):
        tps = [d for d in docs if d.get("kind") in ("TracingPolicyNamespaced", "TracingPolicy")
               and "svc-bundles-executor" in d["metadata"]["name"]]
        cnps = _kinds(docs, "CiliumNetworkPolicy")
        assert len(cnps) == 1, f"{label}: expected 1 executor CNP, examined {len(cnps)}"
        assert len(tps) == want_tp, f"{label}: expected {want_tp} executor TracingPolicy, got {len(tps)}"
    # Every OTHER workload keeps its Tetragon policy when the executor goes Kata.
    other_off = [d["metadata"]["name"] for d in off if d.get("kind") == "TracingPolicyNamespaced"
                 and "svc-bundles-executor" not in d["metadata"]["name"]]
    other_on = [d["metadata"]["name"] for d in on if d.get("kind") == "TracingPolicyNamespaced"
                and "svc-bundles-executor" not in d["metadata"]["name"]]
    assert other_off == other_on, (len(other_off), len(other_on))
    if off_tp:
        assert other_off, "no non-executor Tetragon policies examined"


def _env_value(sts: dict[str, Any], name: str) -> str:
    """Value of one env var on the executor container."""
    env = {e["name"]: e for e in sts["spec"]["template"]["spec"]["containers"][0]["env"]}
    return env[name]["value"]


def test_kata_override_budget_below_its_own_limit() -> None:
    """Kata override replaces resources AND budget; the budget is checked against the new LIMIT."""
    sts = _kinds(_docs("values-dal2alpha.yaml", ["bundleExecutor.runtimeClass.enabled=true"]), "StatefulSet")
    assert len(sts) == 1, f"examined {len(sts)} executor StatefulSets"
    assert _env_value(sts[0], "EXECUTOR_MEMORY_BUDGET_MB") == "512"
    assert sts[0]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "1Gi"
    res = _render("values-dal2alpha.yaml", ["bundleExecutor.runtimeClass.enabled=true",
                                            "bundleExecutor.runtimeClass.memoryBudgetMb=1024"])
    assert res.returncode != 0 and "below the memory limit" in res.stderr, res.stderr


def test_kata_applies_to_every_pool() -> None:
    """With two pools (both language classes serving both stages) every pool is a Kata pod."""
    sets = ["bundleExecutor.runtimeClass.enabled=true",
            "pipeline.rustDataPlane.bundleExecutor.pools[0].name=native",
            "pipeline.rustDataPlane.bundleExecutor.pools[0].languageClass=compiled",
            "pipeline.rustDataPlane.bundleExecutor.pools[0].stages[0]=svc-process-rust",
            "pipeline.rustDataPlane.bundleExecutor.pools[0].stages[1]=svc-action-rust",
            "pipeline.rustDataPlane.bundleExecutor.pools[1].name=script",
            "pipeline.rustDataPlane.bundleExecutor.pools[1].languageClass=scripted",
            "pipeline.rustDataPlane.bundleExecutor.pools[1].stages[0]=svc-process-rust",
            "pipeline.rustDataPlane.bundleExecutor.pools[1].stages[1]=svc-action-rust"]
    sts = _kinds(_docs("values-dal2alpha.yaml", sets), "StatefulSet")
    assert len(sts) == 2, f"examined {len(sts)} executor StatefulSets"
    for d in sts:
        assert d["spec"]["template"]["spec"]["runtimeClassName"] == "kata"
