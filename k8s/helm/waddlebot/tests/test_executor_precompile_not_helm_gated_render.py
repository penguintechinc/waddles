"""Regression (alpha deploy 43868192527): bundle precompile must never gate `helm upgrade`.

The old executor-precompile-backfill post-upgrade HOOK compiled ~160 bundles serially (~70 s per
componentize-py bundle) inside Helm's wait and timed the release out, while 5-6 overlapping
CronJob runs compiled the same digests. Pinned here:

- no precompile document carries a helm.sh/hook annotation; the per-release enqueue Job is a plain
  Job named per revision that only runs `enqueue-all-active`;
- the CronJob is concurrencyPolicy Forbid with a startingDeadline, bounded history, and an
  activeDeadlineSeconds below the 1800 s queue lease (render fails otherwise);
- the compile step gets --compile-concurrency from values, and the render fails when that bound
  does not fit the compile container's CPU/memory limits;
- the compile step's PRECOMPILE_SIGNING_PUBLIC_KEYS (needed to skip already-built artifacts) is
  required, not optional.

Every test asserts how many documents it examined -- zero is a failure, and a missing helm CLI is
a failure, not a skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_ENVS = (
    "values-dal2alpha.yaml",
    "values-alpha.yaml",
    "values-beta.yaml",
    "values-production.yaml",
)
_P = "pipeline.hubApi.executorPrecompile"


def _helm(
    values: str, extra_set: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    args = [
        "helm",
        "template",
        "waddlebot",
        str(_CHART_DIR),
        "--kube-version",
        "1.30.0",
        "--values",
        str(_CHART_DIR / values),
        "--set",
        "global.deploymentTier=alpha",
    ]
    for kv in extra_set or []:
        args += ["--set", kv]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _render(values: str, extra_set: list[str] | None = None) -> list[dict[str, Any]]:
    result = _helm(values, extra_set)
    if result.returncode != 0:
        pytest.fail(f"helm template failed for {values}:\n{result.stderr}")
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    assert docs, f"helm template produced zero documents for {values}"
    return docs


def _precompile_workloads(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        d
        for d in docs
        if d["kind"] in ("Job", "CronJob")
        and "executor-precompile" in d["metadata"]["name"]
    ]


def _cronjob(docs: list[dict[str, Any]]) -> dict[str, Any]:
    cjs = [
        d
        for d in docs
        if d["kind"] == "CronJob"
        and d["metadata"]["name"].endswith("-executor-precompile")
    ]
    assert len(cjs) == 1, f"examined {len(cjs)} precompile CronJobs"
    return cjs[0]


def _compile_container(cj: dict[str, Any]) -> dict[str, Any]:
    spec = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return next(c for c in spec["initContainers"] if c["name"] == "compile")


@pytest.mark.parametrize("values", _ENVS)
def test_no_precompile_workload_is_a_helm_hook(values: str) -> None:
    workloads = _precompile_workloads(_render(values))
    kinds = sorted(d["kind"] for d in workloads)
    assert kinds == ["CronJob", "Job"], f"examined {len(workloads)}: {kinds}"
    for d in workloads:
        annotations = d["metadata"].get("annotations") or {}
        hooks = [k for k in annotations if k.startswith("helm.sh/hook")]
        assert not hooks, f"{d['metadata']['name']} is a Helm hook: {hooks}"
    names = [d["metadata"]["name"] for d in workloads]
    assert not any(n.endswith("-executor-precompile-backfill") for n in names), names


@pytest.mark.parametrize("values", _ENVS)
def test_enqueue_job_is_per_revision_and_only_enqueues(values: str) -> None:
    jobs = [
        d
        for d in _render(values)
        if d["kind"] == "Job"
        and "-executor-precompile-enqueue-r" in d["metadata"]["name"]
    ]
    assert len(jobs) == 1, f"examined {len(jobs)} enqueue Jobs"
    job = jobs[0]
    assert job["metadata"]["name"].endswith("-enqueue-r1")
    assert job["metadata"]["labels"]["waddlebot.io/helm-revision"] == "1"
    spec = job["spec"]
    assert spec["ttlSecondsAfterFinished"] > 0
    assert spec["activeDeadlineSeconds"] > 0
    pod = spec["template"]["spec"]
    assert "initContainers" not in pod
    (container,) = pod["containers"]
    assert container["command"] == [
        "python3",
        "-m",
        "cli.precompile_queue",
        "enqueue-all-active",
    ]


@pytest.mark.parametrize("values", _ENVS)
def test_cronjob_never_overlaps_and_cannot_pile_up(values: str) -> None:
    cj = _cronjob(_render(values))
    spec = cj["spec"]
    assert spec["concurrencyPolicy"] == "Forbid"
    assert 0 < spec["startingDeadlineSeconds"] <= 300
    assert 0 < spec["successfulJobsHistoryLimit"] <= 5
    assert 0 < spec["failedJobsHistoryLimit"] <= 5
    # The smoke step selects this CronJob's Jobs by label to fail on a failed batch.
    labels = spec["jobTemplate"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/component"] == "executor-precompile"
    deadline = spec["jobTemplate"]["spec"]["activeDeadlineSeconds"]
    assert 0 < deadline < 1800, (
        f"activeDeadlineSeconds {deadline} must be below the 1800 s lease"
    )


def test_concurrency_policy_is_not_overridable() -> None:
    cj = _cronjob(_render("values-dal2alpha.yaml", [f"{_P}.concurrencyPolicy=Allow"]))
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"


def test_deadline_at_or_over_lease_fails_the_render() -> None:
    result = _helm("values-dal2alpha.yaml", [f"{_P}.activeDeadlineSeconds=1800"])
    assert result.returncode != 0
    assert "queue lease" in result.stderr


@pytest.mark.parametrize("values", _ENVS)
def test_compile_step_gets_bounded_concurrency_and_required_public_keys(
    values: str,
) -> None:
    compile_c = _compile_container(_cronjob(_render(values)))
    args = compile_c["args"]
    assert args[args.index("--compile-concurrency") + 1] == "3"
    env = {e["name"]: e for e in compile_c["env"]}
    ref = env["PRECOMPILE_SIGNING_PUBLIC_KEYS"]["valueFrom"]["configMapKeyRef"]
    assert not ref.get("optional", False), (
        "public keys must be required (skip already-built)"
    )


def test_compile_concurrency_value_flows_to_args() -> None:
    compile_c = _compile_container(
        _cronjob(_render("values-dal2alpha.yaml", [f"{_P}.compileConcurrency=4"]))
    )
    args = compile_c["args"]
    assert args[args.index("--compile-concurrency") + 1] == "4"


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ([f"{_P}.compileConcurrency=0"], "must be 1..32"),
        ([f"{_P}.compileConcurrency=5"], "exceeds resources.limits.cpu"),
        ([f"{_P}.resources.limits.cpu=2000m"], "exceeds resources.limits.cpu"),
        (
            [f"{_P}.compileConcurrency=4", f"{_P}.compileMemoryPerWorkerMi=4096"],
            "exceeds resources.limits.memory",
        ),
        ([f"{_P}.resources.limits.memory=4096Mi"], "exceeds resources.limits.memory"),
    ],
)
def test_concurrency_that_does_not_fit_the_pod_fails_the_render(
    extra: list[str], needle: str
) -> None:
    result = _helm("values-dal2alpha.yaml", extra)
    assert result.returncode != 0, f"render unexpectedly succeeded with {extra}"
    assert needle in result.stderr, result.stderr


def test_removed_backfill_values_are_gone() -> None:
    values = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())
    cfg = values["pipeline"]["hubApi"]["executorPrecompile"]
    assert "backfill" not in cfg
    assert "concurrencyPolicy" not in cfg
    assert cfg["enqueue"]["enabled"] is True
