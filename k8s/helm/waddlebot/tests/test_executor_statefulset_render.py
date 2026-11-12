"""Helm-template assertions for executor Phase 1 infra (feature/executor-phase1-infra).

Pins: svc-bundles-executor is a StatefulSet per pool (pod name = executor_id for rendezvous
slot sharding) with a port-less headless Service, PDB maxUnavailable 1, EXECUTOR_ID /
EXECUTOR_POOL / EXECUTOR_MEMORY_BUDGET_MB env, KEDA ScaledObject or HPA-external autoscaling
that FAIL LOUD when the cluster lacks the API, and values-driven OTLP exporter env on the
executor + both Rust stages (never hardcoded).

Every test asserts how many documents/workloads it examined -- zero is a failure, never a pass
(critical-rules.md Verification Integrity). A missing helm CLI fails, it does not skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parents[1]
_EXEC = "waddlebot-svc-bundles-executor"
_VALUES = "values-dal2alpha.yaml"


def _helm(extra_set: list[str], api_versions: list[str] | None = None, values: str = _VALUES) -> subprocess.CompletedProcess[str]:
    """Run `helm template` with the given values file, --set overrides and --api-versions."""
    args = ["helm", "template", "waddlebot", str(_CHART_DIR), "--kube-version", "1.30.0",
            "--values", str(_CHART_DIR / values)]
    for kv in extra_set:
        args += ["--set", kv]
    for av in api_versions or []:
        args += ["--api-versions", av]
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, test-only


def _render(extra_set: list[str] | None = None, api_versions: list[str] | None = None, values: str = _VALUES) -> list[dict[str, Any]]:
    """Render + parse; a render failure or zero documents fails the test."""
    res = _helm(extra_set or [], api_versions, values)
    if res.returncode != 0:
        pytest.fail(f"helm template failed:\n{res.stderr}")
    docs = [d for d in yaml.safe_load_all(res.stdout) if d]
    assert docs, "zero documents rendered"
    return docs


def _render_fails(extra_set: list[str], needle: str, api_versions: list[str] | None = None) -> None:
    """Assert the render fails and the error names the reason."""
    res = _helm(extra_set, api_versions)
    assert res.returncode != 0, "render unexpectedly succeeded"
    assert needle in res.stderr, res.stderr


def _executors(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every workload (any kind) carrying the executor component label."""
    out = [d for d in docs if d["kind"] in ("Deployment", "StatefulSet")
           and d["metadata"]["labels"].get("app.kubernetes.io/component") == "svc-bundles-executor"]
    assert out, "no svc-bundles-executor workload rendered"
    return out


def _env(workload: dict[str, Any], container: str) -> dict[str, Any]:
    """Env list of one container as name -> entry."""
    cs = [c for c in workload["spec"]["template"]["spec"]["containers"] if c["name"] == container]
    assert len(cs) == 1, f"container {container} not found exactly once"
    return {e["name"]: e for e in cs[0].get("env", [])}


def _named(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """The one document of `kind` named `name`."""
    hits = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(hits) == 1, f"{kind}/{name}: {len(hits)} found"
    return hits[0]


def test_dal2alpha_executor_is_statefulset_with_headless_service() -> None:
    docs = _render()
    ex = _executors(docs)
    assert [(d["kind"], d["metadata"]["name"]) for d in ex] == [("StatefulSet", _EXEC)]
    sts = ex[0]
    assert sts["spec"]["replicas"] == 2
    assert sts["spec"]["podManagementPolicy"] == "Parallel"
    assert sts["spec"]["serviceName"] == f"{_EXEC}-headless"
    svc = _named(docs, "Service", f"{_EXEC}-headless")
    assert svc["spec"]["clusterIP"] == "None"
    assert "ports" not in svc["spec"], "executor has no listener -- headless Service must be port-less"
    assert svc["spec"]["selector"] == sts["spec"]["selector"]["matchLabels"]
    assert sts["spec"]["selector"]["matchLabels"].items() <= sts["spec"]["template"]["metadata"]["labels"].items()


def test_executor_identity_budget_and_cpu_limit() -> None:
    sts = _executors(_render())[0]
    env = _env(sts, "svc-bundles-executor")
    assert env["EXECUTOR_ID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert env["EXECUTOR_POOL"]["value"] == "default"
    assert env["EXECUTOR_MEMORY_BUDGET_MB"]["value"] == "5120"
    assert env["EXECUTOR_PRECOMPILED_PREFIX"]["value"] == "precompiled"
    assert env["EXECUTOR_MAX_CONCURRENT_COMPILES"]["value"] == "2"
    assert env["STAGE_HOST_API_ADDR"]["value"] == "waddlebot-svc-process-rust:8301,waddlebot-svc-action-rust:8302"
    c = sts["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"]["cpu"] == "4000m"
    assert c["resources"]["limits"]["memory"] == "8Gi"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    vols = {v["name"]: v for v in sts["spec"]["template"]["spec"]["volumes"]}
    assert vols["precompile-cache"]["emptyDir"]["sizeLimit"] == "4Gi"


def test_pdb_max_unavailable_one_and_no_legacy_hpa() -> None:
    docs = _render()
    pdb = _named(docs, "PodDisruptionBudget", _EXEC)
    assert pdb["spec"]["maxUnavailable"] == 1
    assert pdb["spec"]["selector"]["matchLabels"]["waddles.io/executor-pool"] == "default"
    hpas = [d for d in docs if d["kind"] == "HorizontalPodAutoscaler" and "bundles-executor" in d["metadata"]["name"]]
    assert hpas == [], "dead cpu/mem HPA must not render for the executor"
    assert not any(d["kind"] == "Deployment" and d["metadata"]["name"] == _EXEC for d in docs)


def test_policies_still_select_statefulset_pods() -> None:
    docs = _render()
    labels = _executors(docs)[0]["spec"]["template"]["metadata"]["labels"]
    cnps = [d for d in docs if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"] == _EXEC]
    tps = [d for d in docs if d["kind"] in ("TracingPolicyNamespaced", "TracingPolicy")
           and "svc-bundles-executor" in d["metadata"]["name"]]
    assert len(cnps) == 1, len(cnps)
    assert cnps[0]["spec"]["endpointSelector"]["matchLabels"].items() <= labels.items()
    assert tps, "no Tetragon policy for the executor"
    for tp in tps:
        sel = tp["spec"]["podSelector"]["matchLabels"]
        assert sel.items() <= labels.items(), sel


def test_otlp_endpoint_absent_without_collector_but_service_name_set() -> None:
    docs = _render()
    checked = 0
    for name, container in ((_EXEC, "svc-bundles-executor"),
                            ("waddlebot-svc-process-rust", "svc-process-rust"),
                            ("waddlebot-svc-action-rust", "svc-action-rust")):
        wl = next(d for d in docs if d["kind"] in ("Deployment", "StatefulSet") and d["metadata"]["name"] == name)
        env = _env(wl, container)
        assert "OTEL_SERVICE_NAME" in env
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
        checked += 1
    assert checked == 3


def test_otlp_endpoint_from_values_on_executor_and_stages() -> None:
    docs = _render(["otel.exporter.endpoint=http://otel.example.svc:4317"])
    checked = 0
    for name, container in ((_EXEC, "svc-bundles-executor"),
                            ("waddlebot-svc-process-rust", "svc-process-rust"),
                            ("waddlebot-svc-action-rust", "svc-action-rust")):
        wl = next(d for d in docs if d["kind"] in ("Deployment", "StatefulSet") and d["metadata"]["name"] == name)
        env = _env(wl, container)
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://otel.example.svc:4317"
        assert env["OTEL_EXPORTER_OTLP_PROTOCOL"]["value"] == "grpc"
        checked += 1
    assert checked == 3


def test_otlp_collector_enabled_derives_endpoint_and_cnp_egress() -> None:
    docs = _render(["otel.collector.enabled=true"])
    env = _env(_executors(docs)[0], "svc-bundles-executor")
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://waddlebot-otel-collector:4317"
    cnp = _named(docs, "CiliumNetworkPolicy", _EXEC)
    otel = [r for r in cnp["spec"]["egress"]
            if r.get("toEndpoints", [{}])[0].get("matchLabels", {}).get("app.kubernetes.io/component") == "otel-collector"]
    assert len(otel) == 1
    assert otel[0]["toPorts"][0]["ports"] == [{"port": "4317", "protocol": "TCP"}]


_KEDA = ["pipeline.rustDataPlane.bundleExecutor.autoscaling.mode=keda",
         "pipeline.rustDataPlane.bundleExecutor.autoscaling.prometheus.serverAddress=http://prom:9090"]


def test_keda_fails_loud_without_crd() -> None:
    _render_fails(_KEDA, "no keda.sh/v1alpha1 ScaledObject CRD")


def test_keda_scaledobject_targets_statefulset() -> None:
    docs = _render(_KEDA, ["keda.sh/v1alpha1/ScaledObject"])
    so = _named(docs, "ScaledObject", _EXEC)
    assert so["spec"]["scaleTargetRef"] == {"apiVersion": "apps/v1", "kind": "StatefulSet", "name": _EXEC}
    assert so["spec"]["minReplicaCount"] == 2
    assert so["spec"]["cooldownPeriod"] == 600
    trig = {t["name"]: t for t in so["spec"]["triggers"]}
    assert set(trig) == {"queue-wait-p95", "pool-exhausted-rate"}
    assert 'queue_wait_seconds_bucket{executor_pool="default"}' in trig["queue-wait-p95"]["metadata"]["query"]
    assert 'pool_exhausted_total{executor_pool="default"}' in trig["pool-exhausted-rate"]["metadata"]["query"]
    assert "replicas" not in _executors(docs)[0]["spec"], "autoscaler owns replicas"
    assert _named(docs, "PodDisruptionBudget", _EXEC)["spec"]["maxUnavailable"] == 1


def test_keda_requires_server_address_and_min_two() -> None:
    _render_fails(_KEDA[:1], "serverAddress is required", ["keda.sh/v1alpha1/ScaledObject"])
    _render_fails(_KEDA + ["pipeline.rustDataPlane.bundleExecutor.autoscaling.minReplicas=1"],
                  "must be >= 2", ["keda.sh/v1alpha1/ScaledObject"])


def test_hpa_external_fallback() -> None:
    mode = ["pipeline.rustDataPlane.bundleExecutor.autoscaling.mode=hpa-external"]
    _render_fails(mode, "no external.metrics.k8s.io/v1beta1")
    docs = _render(mode, ["external.metrics.k8s.io/v1beta1"])
    hpa = _named(docs, "HorizontalPodAutoscaler", _EXEC)
    assert hpa["spec"]["scaleTargetRef"]["kind"] == "StatefulSet"
    assert [m["type"] for m in hpa["spec"]["metrics"]] == ["External", "External"]
    assert all(m["external"]["metric"]["selector"]["matchLabels"]["executor_pool"] == "default" for m in hpa["spec"]["metrics"])


def test_unknown_mode_and_budget_over_limit_fail() -> None:
    _render_fails(["pipeline.rustDataPlane.bundleExecutor.autoscaling.mode=cpu"], "must be none, keda or hpa-external")
    _render_fails(["pipeline.rustDataPlane.bundleExecutor.memoryBudgetMb=8192"], "must be >0 and below the memory limit")


def test_per_stage_pools() -> None:
    docs = _render([
        "pipeline.rustDataPlane.bundleExecutor.pools[0].name=process",
        "pipeline.rustDataPlane.bundleExecutor.pools[0].stages[0]=svc-process-rust",
        "pipeline.rustDataPlane.bundleExecutor.pools[1].name=action",
        "pipeline.rustDataPlane.bundleExecutor.pools[1].stages[0]=svc-action-rust",
        "pipeline.rustDataPlane.bundleExecutor.pools[1].replicas=3",
    ])
    ex = {d["metadata"]["name"]: d for d in _executors(docs)}
    assert set(ex) == {f"{_EXEC}-process", f"{_EXEC}-action"}
    assert _env(ex[f"{_EXEC}-process"], "svc-bundles-executor")["STAGE_HOST_API_ADDR"]["value"] == "waddlebot-svc-process-rust:8301"
    assert _env(ex[f"{_EXEC}-action"], "svc-bundles-executor")["STAGE_HOST_API_ADDR"]["value"] == "waddlebot-svc-action-rust:8302"
    assert ex[f"{_EXEC}-action"]["spec"]["replicas"] == 3
    assert _env(ex[f"{_EXEC}-action"], "svc-bundles-executor")["EXECUTOR_POOL"]["value"] == "action"
    pdbs = {d["metadata"]["name"] for d in docs if d["kind"] == "PodDisruptionBudget" and "bundles-executor" in d["metadata"]["name"]}
    assert pdbs == {f"{_EXEC}-process", f"{_EXEC}-action"}


def test_pools_must_cover_each_stage_exactly_once() -> None:
    _render_fails([
        "pipeline.rustDataPlane.bundleExecutor.pools[0].name=process",
        "pipeline.rustDataPlane.bundleExecutor.pools[0].stages[0]=svc-process-rust",
    ], "is not served by any pool")
    _render_fails([
        "pipeline.rustDataPlane.bundleExecutor.pools[0].name=a",
        "pipeline.rustDataPlane.bundleExecutor.pools[0].stages[0]=svc-process-rust",
        "pipeline.rustDataPlane.bundleExecutor.pools[0].stages[1]=svc-action-rust",
        "pipeline.rustDataPlane.bundleExecutor.pools[1].name=b",
        "pipeline.rustDataPlane.bundleExecutor.pools[1].stages[0]=svc-action-rust",
    ], "assigned to more than one pool")


def test_executor_env_contract_pr1253() -> None:
    """PR #1253 env contract: require-id, downward-API POD_NAME, precompile verify keys."""
    env = _env(_executors(_render())[0], "svc-bundles-executor")
    assert env["EXECUTOR_REQUIRE_ID"]["value"] == "true"
    assert env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    ref = env["PRECOMPILE_SIGNING_PUBLIC_KEYS"]["valueFrom"]["configMapKeyRef"]
    assert ref == {"name": "waddlebot-executor-precompile-signing-public",
                   "key": "PRECOMPILE_SIGNING_PUBLIC_KEYS", "optional": False}
    assert "BUNDLE_MAX_PRECOMPILED_BYTES" not in env, "unset value must not render the env"


def test_max_precompiled_bytes_optional_value() -> None:
    """Setting maxPrecompiledBytes renders BUNDLE_MAX_PRECOMPILED_BYTES verbatim."""
    env = _env(_executors(_render(
        ["pipeline.rustDataPlane.bundleExecutor.maxPrecompiledBytes=536870912"]))[0], "svc-bundles-executor")
    assert env["BUNDLE_MAX_PRECOMPILED_BYTES"]["value"] == "536870912"


def test_default_values_budget_below_limit() -> None:
    """Base values.yaml: budget 8192 (crate default) with headroom under a 12Gi limit.

    Parsed directly: base values alone are not renderable outside alpha (seaweedfs/valkey
    fail-loud guards), and the render-time budget<limit guard is covered by dal2alpha tests.
    """
    be = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())["pipeline"]["rustDataPlane"]["bundleExecutor"]
    assert be["memoryBudgetMb"] == 8192
    assert be["resources"]["limits"]["memory"] == "12Gi"
    assert be["memoryBudgetMb"] < 12 * 1024 * 0.75, "budget must leave >=25% headroom under the limit"
    assert be["maxPrecompiledBytes"] == ""
