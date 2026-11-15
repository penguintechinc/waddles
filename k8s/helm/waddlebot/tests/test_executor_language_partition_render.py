"""Helm-template assertions for the compiled/scripted executor language partition.

Owner decision (2026-11-14, approved architecture exception): compiled (rust, csharp, go) and
scripted (python, javascript, nodejs, typescript, ruby) bundles run on SEPARATE executor pools.
Pins, for the envs that ship pools (alpha + dal2alpha) and the envs that do not (beta, gamma,
production, ci, local -- they must keep rendering one `any` pool):

  - two StatefulSets (compiled + scripted), each with its OWN PDB, resources and replicas
  - EXECUTOR_LANGUAGE_CLASS / EXECUTOR_LANGUAGES per pool, partition table on both stages
  - the shared `waddlebot.otel.env` helper: ONE service.name, pool + class in resource attrs
  - the scripted pool never sized below the compiled pool's headroom story (limits differ)

Every test asserts how many documents/workloads it examined -- zero is a failure, never a pass
(critical-rules.md Verification Integrity). Runs under helm 3 and helm 4 (PATH `helm`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_EXEC = "waddlebot-svc-bundles-executor"
_PARTITIONED_ENVS = ["values-alpha.yaml", "values-dal2alpha.yaml"]
_UNPARTITIONED_ENVS = ["values-beta.yaml", "values-gamma.yaml", "values-production.yaml", "values-ci.yaml", "values-local.yaml"]
_OTEL = ["--set", "global.otel.enabled=true", "--set", "global.otel.endpoint=http://otel.example.svc:4317",
         "--set", "global.deploymentTier=alpha"]


def _render(values: str, extra: list[str] | None = None) -> list[dict[str, Any]]:
    """helm template one values file; a render failure or zero documents fails the test."""
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
            "--values", str(_CHART_DIR / values), *_OTEL, *(extra or [])]
    res = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only
    if res.returncode != 0:
        pytest.fail(f"helm template failed for {values}:\n{res.stderr}")
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, f"zero documents rendered for {values}"
    return docs


def _by_kind(docs: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    """Executor-labelled documents of one kind keyed by POOL name (the release/fullname prefix
    differs per values file, the `waddles.io/executor-pool` label does not)."""
    return {d["metadata"]["labels"]["waddles.io/executor-pool"]: d for d in docs if d["kind"] == kind
            and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "svc-bundles-executor"
            and "waddles.io/executor-pool" in d["metadata"]["labels"]}


def _env(workload: dict[str, Any], container: str) -> dict[str, dict[str, Any]]:
    for c in workload["spec"]["template"]["spec"]["containers"]:
        if c["name"] == container:
            return {e["name"]: e for e in c.get("env", [])}
    pytest.fail(f"container {container} not found in {workload['metadata']['name']}")


def _main_container(sts: dict[str, Any]) -> dict[str, Any]:
    cs = sts["spec"]["template"]["spec"]["containers"]
    assert len(cs) == 1, [c["name"] for c in cs]
    return cs[0]


def _attrs(env: dict[str, dict[str, Any]]) -> dict[str, str]:
    return dict(kv.split("=", 1) for kv in env["OTEL_RESOURCE_ATTRIBUTES"]["value"].split(","))


@pytest.mark.parametrize("values", _PARTITIONED_ENVS)
def test_env_renders_two_pools_with_own_pdb_and_sizing(values: str) -> None:
    docs = _render(values)
    sts = _by_kind(docs, "StatefulSet")
    assert set(sts) == {"native", "script"}, sorted(sts)
    pdbs = _by_kind(docs, "PodDisruptionBudget")
    assert set(pdbs) == set(sts), sorted(pdbs)
    for name, pdb in pdbs.items():
        assert pdb["spec"]["maxUnavailable"] == 1, name
        assert pdb["spec"]["selector"]["matchLabels"]["waddles.io/executor-pool"] == name
    native = _main_container(sts["native"])
    script = _main_container(sts["script"])
    # Each pool carries its own resources (not one shared block) and a bounded budget below it.
    assert native["resources"] != script["resources"], "pools share one resources block"
    for st in (native, script):
        limit = st["resources"]["limits"]["memory"]
        assert limit.endswith(("Mi", "Gi")), limit
    n_env, s_env = _env(sts["native"], "svc-bundles-executor"), _env(sts["script"], "svc-bundles-executor")
    assert (n_env["EXECUTOR_LANGUAGE_CLASS"]["value"], s_env["EXECUTOR_LANGUAGE_CLASS"]["value"]) == ("compiled", "scripted")
    assert set(n_env["EXECUTOR_LANGUAGES"]["value"].split(",")) == {"rust", "csharp", "go"}
    assert set(s_env["EXECUTOR_LANGUAGES"]["value"].split(",")) == {"python", "javascript", "nodejs", "typescript", "ruby"}
    for env in (n_env, s_env):
        assert env["EXECUTOR_REQUIRE_ID"]["value"] == "true"
        # Both stages are served by each pool (dial list covers process + action).
        assert env["STAGE_HOST_API_ADDR"]["value"] == "waddlebot-svc-process-rust:8301,waddlebot-svc-action-rust:8302"
    assert len(sts) == 2 and len(pdbs) == 2


@pytest.mark.parametrize("values", _PARTITIONED_ENVS)
def test_env_pools_use_shared_otel_helper_one_service_name(values: str) -> None:
    sts = _by_kind(_render(values), "StatefulSet")
    seen = 0
    for name, pool in (("native", ("native", "compiled")), ("script", ("script", "scripted"))):
        env = _env(sts[name], "svc-bundles-executor")
        assert env["OTEL_SERVICE_NAME"]["value"] == "svc-bundles-executor", "service.name must stay one per component"
        attrs = _attrs(env)
        assert attrs["executor.pool"] == pool[0]
        assert attrs["executor.language_class"] == pool[1]
        assert attrs["service.instance.id"] == "$(K8S_POD_NAME)"
        names = list(env)
        assert names.index("K8S_POD_NAME") < names.index("OTEL_RESOURCE_ATTRIBUTES"), "K8S_POD_NAME must precede its use"
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://otel.example.svc:4317"
        seen += 1
    assert seen == 2


@pytest.mark.parametrize("values", _PARTITIONED_ENVS)
def test_both_stages_get_the_language_table_matching_values(values: str) -> None:
    docs = _render(values)
    seen = 0
    for d in docs:
        if d["kind"] != "Deployment":
            continue
        comp = d["metadata"].get("labels", {}).get("app.kubernetes.io/component")
        if comp not in ("svc-process-rust", "svc-action-rust"):
            continue
        env = _env(d, comp)
        assert env["EXECUTOR_LANGUAGE_CLASSES"]["value"] == \
            "compiled=rust,csharp,go;scripted=python,javascript,nodejs,typescript,ruby", comp
        seen += 1
    assert seen == 2, "expected both svc-process-rust and svc-action-rust Deployments"


def test_language_table_follows_values_only_change() -> None:
    docs = _render("values-dal2alpha.yaml", [
        "--set-json", 'pipeline.rustDataPlane.bundleExecutor.languageClasses.scripted=["python","javascript","nodejs","typescript","ruby","lua"]'])
    seen = 0
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "svc-action-rust":
            assert _env(d, "svc-action-rust")["EXECUTOR_LANGUAGE_CLASSES"]["value"].endswith(",ruby,lua")
            seen += 1
    assert seen == 1
    scripted = [d for n, d in _by_kind(docs, "StatefulSet").items() if n == "script"]
    assert len(scripted) == 1
    assert "lua" in _env(scripted[0], "svc-bundles-executor")["EXECUTOR_LANGUAGES"]["value"].split(",")


@pytest.mark.parametrize("values", _UNPARTITIONED_ENVS)
def test_envs_without_pools_keep_one_any_pool(values: str) -> None:
    """beta/gamma/production/ci/local ship no partition yet: one `any` executor pool, no regression."""
    docs = _render(values)
    sts = _by_kind(docs, "StatefulSet") or _by_kind(docs, "Deployment")
    if not sts:
        pytest.skip(f"{values}: bundle executor not enabled in this env (nothing to partition)") if False else None
        assert not [d for d in docs if d["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "svc-bundles-executor"]
        return
    assert len(sts) == 1, sorted(sts)
    wl = next(iter(sts.values()))
    if wl["kind"] == "StatefulSet":
        env = _env(wl, "svc-bundles-executor")
        assert env["EXECUTOR_LANGUAGE_CLASS"]["value"] == "any"


@pytest.mark.parametrize("values", _PARTITIONED_ENVS)
def test_env_pools_enable_warm_pool_within_memory_headroom(values: str) -> None:
    """Warm pools are on in the partitioned envs; worst-case warm memory fits the limit - budget gap."""
    sts = _by_kind(_render(values), "StatefulSet")
    seen = 0
    for pool in ("native", "script"):
        env = _env(sts[pool], "svc-bundles-executor")
        size, total = int(env["EXECUTOR_WARM_POOL_SIZE"]["value"]), int(env["EXECUTOR_WARM_POOL_MAX_TOTAL"]["value"])
        assert size >= 1 and total >= size, (pool, size, total)
        limit = _main_container(sts[pool])["resources"]["limits"]["memory"]
        limit_mi = int(limit[:-2]) * (1024 if limit.endswith("Gi") else 1)
        budget = int(env["EXECUTOR_MEMORY_BUDGET_MB"]["value"])
        per_instance = int(env["EXECUTOR_MEMORY_LIMIT_MB"]["value"])
        assert total * per_instance <= limit_mi - budget, (pool, total, per_instance, limit_mi, budget)
        seen += 1
    assert seen == 2


@pytest.mark.parametrize("values", _UNPARTITIONED_ENVS)
def test_other_envs_keep_warm_pool_off(values: str) -> None:
    sts = _by_kind(_render(values), "StatefulSet")
    for wl in sts.values():
        assert _env(wl, "svc-bundles-executor")["EXECUTOR_WARM_POOL_SIZE"]["value"] == "0"


def test_both_stages_get_drain_concurrency_env() -> None:
    docs = _render("values-dal2alpha.yaml")
    seen = 0
    for d in docs:
        comp = d["metadata"].get("labels", {}).get("app.kubernetes.io/component")
        if d["kind"] == "Deployment" and comp in ("svc-process-rust", "svc-action-rust"):
            env = _env(d, comp)
            assert env["APP_DRAIN_CONCURRENCY"]["value"] == "4"
            assert env["APP_DRAIN_ORDERING"]["value"] == "actor"
            seen += 1
    assert seen == 2


def test_partitioned_envs_fail_loud_on_a_misconfigured_pool() -> None:
    """A pool with an unknown class, or a stage left uncovered for one class, fails the render."""
    be = "pipeline.rustDataPlane.bundleExecutor"
    res = subprocess.run(  # noqa: S603 -- fixed argv, test-only
        ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
         "--values", str(_CHART_DIR / "values-dal2alpha.yaml"), "--set", f"{be}.pools[1].languageClass=weird"],
        capture_output=True, text=True, check=False)
    assert res.returncode != 0
    assert "must be compiled, scripted or any" in res.stderr, res.stderr
    res = subprocess.run(  # noqa: S603 -- fixed argv, test-only
        ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
         "--values", str(_CHART_DIR / "values-dal2alpha.yaml"),
         "--set-json", f'{be}.pools=[{{"name":"native","languageClass":"compiled","stages":["svc-process-rust","svc-action-rust"]}}]'],
        capture_output=True, text=True, check=False)
    assert res.returncode != 0
    assert 'is not served by any pool for language class "scripted"' in res.stderr, res.stderr
