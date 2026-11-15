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
        # "json:" prefix -> --set-json (helm's --set REPLACES a list with a sparse one on index).
        args += ["--set-json", kv[5:]] if kv.startswith("json:") else ["--set", kv]
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
    assert env["EXECUTOR_MEMORY_BUDGET_MB"]["value"] == "15360"
    assert env["EXECUTOR_PRECOMPILED_PREFIX"]["value"] == "precompiled"
    assert env["EXECUTOR_MAX_CONCURRENT_COMPILES"]["value"] == "2"
    assert env["STAGE_HOST_API_ADDR"]["value"] == "waddlebot-svc-process-rust:8301,waddlebot-svc-action-rust:8302"
    c = sts["spec"]["template"]["spec"]["containers"][0]
    # dal2alpha keeps release's per-pod alpha sizing (8 CPU / 24Gi, #1305); requests are minimal.
    assert c["resources"]["limits"]["cpu"] == "8000m"
    assert c["resources"]["limits"]["memory"] == "24Gi"
    assert c["resources"]["requests"] == {"cpu": "500m", "memory": "1Gi"}
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


_LEGACY = "global.otel.enabled=false"  # the legacy otel.exporter/otel.collector path only renders when global.otel is off


def test_no_otel_env_without_any_collector_or_global_otel() -> None:
    """Neither global.otel nor a legacy endpoint => no OTEL_* env on the executor or stages (no half-wired exporter)."""
    docs = _render([_LEGACY])
    checked = 0
    for name, container in ((_EXEC, "svc-bundles-executor"),
                            ("waddlebot-svc-process-rust", "svc-process-rust"),
                            ("waddlebot-svc-action-rust", "svc-action-rust")):
        wl = next(d for d in docs if d["kind"] in ("Deployment", "StatefulSet") and d["metadata"]["name"] == name)
        env = _env(wl, container)
        assert not [n for n in env if n.startswith("OTEL_") or n == "K8S_POD_NAME"], sorted(env)
        checked += 1
    assert checked == 3


def test_global_otel_enabled_gives_exactly_one_otel_service_name() -> None:
    """global.otel on => the shared helper is the only source (legacy helper must not duplicate env)."""
    docs = _render()
    checked = 0
    for name, container in ((_EXEC, "svc-bundles-executor"),
                            ("waddlebot-svc-process-rust", "svc-process-rust"),
                            ("waddlebot-svc-action-rust", "svc-action-rust")):
        wl = next(d for d in docs if d["kind"] in ("Deployment", "StatefulSet") and d["metadata"]["name"] == name)
        cs = [c for c in wl["spec"]["template"]["spec"]["containers"] if c["name"] == container]
        names = [e["name"] for e in cs[0]["env"]]
        assert names.count("OTEL_SERVICE_NAME") == 1 and names.count("K8S_POD_NAME") == 1, names
        assert len(names) == len(set(names)), "duplicate env names"
        checked += 1
    assert checked == 3


def test_otlp_endpoint_from_values_on_executor_and_stages() -> None:
    docs = _render([_LEGACY, "otel.exporter.endpoint=http://otel.example.svc:4317"])
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
    docs = _render([_LEGACY, "otel.collector.enabled=true"])
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
    # dal2alpha limit is 24Gi = 24576Mi: a budget equal to it is rejected (below the LIMIT, not the request).
    _render_fails(["pipeline.rustDataPlane.bundleExecutor.memoryBudgetMb=24576"], "must be >0 and below the memory limit")


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


_BE = "pipeline.rustDataPlane.bundleExecutor"
_STAGES = ["svc-process-rust", "svc-action-rust"]


def _mi(q: str) -> int:
    """Quantity with a Gi/Mi suffix in MiB."""
    return int(q[:-2]) * 1024 if q.endswith("Gi") else int(q[:-2])


@pytest.mark.parametrize("values", ["values-alpha.yaml", "values-dal2alpha.yaml", "values-beta.yaml",
                                    "values-gamma.yaml", "values-production.yaml"])
def test_sizing_policy_minimal_requests_budget_below_limit(values: str) -> None:
    """Every overlay: requests are the bare minimum, limits carry headroom, budget < LIMIT."""
    docs = _render(values=values, extra_set=["global.deploymentTier=alpha"] if values != "values-alpha.yaml" and values != "values-dal2alpha.yaml" else [])
    ex = _executors(docs)
    assert len(ex) >= 1, "examined zero executor workloads"
    for sts in ex:
        c = sts["spec"]["template"]["spec"]["containers"][0]
        res = c["resources"]
        assert res["requests"]["cpu"] == "500m" and res["requests"]["memory"] == "1Gi", (values, res)
        assert _mi(res["limits"]["memory"]) > _mi(res["requests"]["memory"])
        budget = int(_env(sts, "svc-bundles-executor")["EXECUTOR_MEMORY_BUDGET_MB"]["value"])
        assert 0 < budget < _mi(res["limits"]["memory"]), (values, budget, res["limits"])


def test_memory_request_above_limit_fails() -> None:
    _render_fails([f"{_BE}.resources.requests.memory=30Gi"], "exceeds the memory limit")


def test_spread_across_nodes_default_and_opt_out() -> None:
    sts = _executors(_render())[0]
    tsc = sts["spec"]["template"]["spec"]["topologySpreadConstraints"]
    assert len(tsc) == 1
    assert tsc[0]["topologyKey"] == "kubernetes.io/hostname"
    assert tsc[0]["whenUnsatisfiable"] == "ScheduleAnyway"
    assert tsc[0]["labelSelector"]["matchLabels"].items() <= sts["spec"]["template"]["metadata"]["labels"].items()
    off = _executors(_render([f"{_BE}.spreadAcrossNodes=false"]))[0]
    assert "topologySpreadConstraints" not in off["spec"]["template"]["spec"]


def test_architecture_exception_marker_in_executor_templates() -> None:
    """The approved-exception comment must sit on the executor workload templates."""
    marker = "ARCHITECTURE EXCEPTION (approved 2026-11-14, revisit)"
    seen = 0
    for name in ("svc-bundles-executor.yaml", "svc-bundles-executor-autoscaling.yaml"):
        text = (_CHART_DIR / "templates" / name).read_text()
        assert marker in text, name
        seen += 1
    assert seen == 2


def _langs(sts: dict[str, Any]) -> tuple[str, list[str]]:
    env = _env(sts, "svc-bundles-executor")
    raw = env["EXECUTOR_LANGUAGES"]["value"]
    return env["EXECUTOR_LANGUAGE_CLASS"]["value"], [x for x in raw.split(",") if x]


def test_default_pool_is_any_class_with_all_languages_including_ruby() -> None:
    cls, langs = _langs(_executors(_render())[0])
    assert cls == "any"
    assert {"rust", "csharp", "go", "python", "javascript", "nodejs", "typescript", "ruby"} == set(langs)
    assert len(langs) == len(set(langs)), "a language rendered twice"


_PARTITIONED = [
    f"{_BE}.pools[0].name=native", f"{_BE}.pools[0].languageClass=compiled",
    f"{_BE}.pools[0].stages[0]=svc-process-rust", f"{_BE}.pools[0].stages[1]=svc-action-rust",
    f"{_BE}.pools[1].name=script", f"{_BE}.pools[1].languageClass=scripted",
    f"{_BE}.pools[1].stages[0]=svc-process-rust", f"{_BE}.pools[1].stages[1]=svc-action-rust",
]


def test_scripted_pool_lists_ruby_and_compiled_pool_does_not() -> None:
    """Both executor classes serve BOTH stages; ruby is scripted only (values-driven)."""
    ex = {d["metadata"]["name"]: d for d in _executors(_render(_PARTITIONED))}
    assert set(ex) == {f"{_EXEC}-native", f"{_EXEC}-script"}
    ncls, nlangs = _langs(ex[f"{_EXEC}-native"])
    scls, slangs = _langs(ex[f"{_EXEC}-script"])
    assert (ncls, scls) == ("compiled", "scripted")
    assert set(nlangs) == {"rust", "csharp", "go"} and "ruby" not in nlangs
    assert set(slangs) == {"python", "javascript", "nodejs", "typescript", "ruby"}
    for sts in ex.values():
        assert _env(sts, "svc-bundles-executor")["STAGE_HOST_API_ADDR"]["value"] == \
            "waddlebot-svc-process-rust:8301,waddlebot-svc-action-rust:8302"


def test_language_added_by_values_only() -> None:
    """A new scripted language needs no template change."""
    docs = _render(_PARTITIONED + [f'json:{_BE}.languageClasses.scripted=["python","javascript","nodejs","typescript","ruby","lua"]'])
    ex = {d["metadata"]["name"]: d for d in _executors(docs)}
    assert "lua" in _langs(ex[f"{_EXEC}-script"])[1]
    assert "lua" not in _langs(ex[f"{_EXEC}-native"])[1]


def test_language_in_two_classes_fails() -> None:
    _render_fails([f'json:{_BE}.languageClasses.compiled=["rust","csharp","go","ruby"]'], "appears in more than one class")
    _render_fails([f'json:{_BE}.languageClasses.scripted=["python","python"]'], "appears in more than one class")
    _render_fails([f'json:{_BE}.languageClasses.scripted=["Ruby!"]'], "must be a lower-case token")
    _render_fails([f'json:{_BE}.languageClasses.interpreted=["lua"]'], "unknown class")


def test_coverage_is_per_language_class() -> None:
    # A scripted pool alone leaves the compiled class uncovered.
    scripted_only = [x.replace("pools[1]", "pools[0]") for x in _PARTITIONED if "pools[1]" in x]
    assert len(scripted_only) == 4, "scripted pool settings not found"
    _render_fails(scripted_only, 'is not served by any pool for language class "compiled"')
    # Two scripted pools on one stage collide; an `any` pool collides with any other pool.
    _render_fails(_PARTITIONED + [f"{_BE}.pools[2].name=more", f"{_BE}.pools[2].languageClass=scripted",
                                  f"{_BE}.pools[2].stages[0]=svc-action-rust"],
                  'assigned to more than one pool for language class "scripted"')
    _render_fails(_PARTITIONED + [f"{_BE}.pools[2].name=wild", f"{_BE}.pools[2].stages[0]=svc-action-rust"],
                  "assigned to more than one pool for language class")
    _render_fails([f"{_BE}.pools[0].name=x", f"{_BE}.pools[0].languageClass=weird",
                   f"{_BE}.pools[0].stages[0]=svc-process-rust"], "must be compiled, scripted or any")


def test_any_pools_still_cover_both_classes() -> None:
    """Existing per-stage pools (no languageClass) keep working: `any` covers both classes."""
    docs = _render([f"{_BE}.pools[0].name=process", f"{_BE}.pools[0].stages[0]=svc-process-rust",
                    f"{_BE}.pools[1].name=action", f"{_BE}.pools[1].stages[0]=svc-action-rust"])
    classes = {_langs(d)[0] for d in _executors(docs)}
    assert classes == {"any"}
